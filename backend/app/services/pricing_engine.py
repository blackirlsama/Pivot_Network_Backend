from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform import (
    ImageArtifact,
    ImageOffer,
    ImageOfferPriceSnapshot,
    Node,
    PriceFeedSnapshot,
    ResourceRateCard,
)
from app.services.pricing_sources import (
    AWS_GPU_INSTANCE_MAP,
    PricingSourceError,
    ProviderRates,
    fetch_aws_ec2_provider_rates,
    fetch_azure_vm_provider_rates,
)


class PricingEngineError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def truncate_to_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def latest_resource_rate_card(db: Session) -> ResourceRateCard | None:
    return db.scalar(select(ResourceRateCard).order_by(ResourceRateCard.id.desc()))


def latest_valid_resource_rate_card(db: Session) -> ResourceRateCard | None:
    return db.scalar(
        select(ResourceRateCard)
        .where(ResourceRateCard.cpu_price_usd_per_hour > 0, ResourceRateCard.ram_price_usd_per_gib_hour > 0)
        .order_by(ResourceRateCard.id.desc())
    )


def _record_price_feed_snapshot(
    db: Session,
    *,
    provider: str,
    region: str,
    source_url: str,
    status: str,
    raw_payload: dict[str, Any],
    error_message: str | None = None,
) -> PriceFeedSnapshot:
    snapshot = PriceFeedSnapshot(
        provider=provider,
        reference_region=region,
        status=status,
        source_url=source_url,
        raw_payload=raw_payload,
        error_message=error_message,
    )
    db.add(snapshot)
    return snapshot


def _combine_provider_rates(rates: list[ProviderRates]) -> tuple[float, float, dict[str, float], dict[str, Any]]:
    if not rates:
        raise PricingEngineError("resource_rate_card_sources_unavailable")

    cpu_rate = sum(item.cpu_price_usd_per_hour for item in rates) / len(rates)
    ram_rate = sum(item.ram_price_usd_per_gib_hour for item in rates) / len(rates)
    gpu_rates: dict[str, float] = {}
    for item in rates:
        for gpu_model, price in item.gpu_price_usd_per_hour.items():
            gpu_rates[gpu_model] = price
    source_summary = {
        item.provider: {
            "region": item.region,
            "cpu_price_usd_per_hour": item.cpu_price_usd_per_hour,
            "ram_price_usd_per_gib_hour": item.ram_price_usd_per_gib_hour,
            "gpu_price_usd_per_hour": item.gpu_price_usd_per_hour,
            "matched_samples": item.matched_samples,
            "source_url": item.source_url,
        }
        for item in rates
    }
    return cpu_rate, ram_rate, gpu_rates, source_summary


def refresh_resource_rate_card(db: Session, now: datetime | None = None) -> ResourceRateCard | None:
    now = now or utcnow()
    providers: list[ProviderRates] = []
    failures: list[str] = []

    try:
        azure_rates = fetch_azure_vm_provider_rates(settings)
        providers.append(azure_rates)
        _record_price_feed_snapshot(
            db,
            provider="azure",
            region=azure_rates.region,
            source_url=azure_rates.source_url,
            status="success",
            raw_payload={"matched_samples": azure_rates.matched_samples},
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"azure:{exc}")
        _record_price_feed_snapshot(
            db,
            provider="azure",
            region=settings.PRICING_REFERENCE_AZURE_REGION,
            source_url=f"https://prices.azure.com/api/retail/prices?region={settings.PRICING_REFERENCE_AZURE_REGION}",
            status="error",
            raw_payload={},
            error_message=str(exc),
        )

    try:
        aws_rates = fetch_aws_ec2_provider_rates(settings)
        providers.append(aws_rates)
        _record_price_feed_snapshot(
            db,
            provider="aws",
            region=aws_rates.region,
            source_url=aws_rates.source_url,
            status="success",
            raw_payload={"matched_samples": aws_rates.matched_samples},
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"aws:{exc}")
        _record_price_feed_snapshot(
            db,
            provider="aws",
            region=settings.PRICING_REFERENCE_AWS_REGION,
            source_url=(
                f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/"
                f"{settings.PRICING_REFERENCE_AWS_REGION}/index.json"
            ),
            status="error",
            raw_payload={},
            error_message=str(exc),
        )

    if not providers:
        previous = latest_valid_resource_rate_card(db)
        if previous is not None and previous.stale_at is None:
            previous.stale_at = now
            db.commit()
        return previous

    cpu_rate, ram_rate, gpu_rates, source_summary = _combine_provider_rates(providers)
    stale_at = now if failures else None
    card = ResourceRateCard(
        status="active" if not failures else "stale",
        effective_hour=truncate_to_hour(now),
        usd_cny_rate=settings.USD_CNY_RATE,
        cpu_price_usd_per_hour=cpu_rate,
        ram_price_usd_per_gib_hour=ram_rate,
        gpu_price_usd_per_hour=gpu_rates,
        source_summary=source_summary,
        stale_at=stale_at,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def offer_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(ImageOffer)) or 0)


def has_gpu_unmapped(measured_capabilities: dict[str, Any], gpu_rates: dict[str, float]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for gpu in measured_capabilities.get("gpus", []):
        model = str(gpu.get("model") or "").strip().lower()
        if model and model not in gpu_rates and model not in AWS_GPU_INSTANCE_MAP:
            missing.append(model)
        elif model and model not in gpu_rates:
            missing.append(model)
    return bool(missing), missing


def _extract_offer_resources(offer: ImageOffer, node: Node) -> dict[str, Any]:
    measured = dict(offer.probe_measured_capabilities or {})
    node_caps = node.capabilities or {}
    cpu_count = int(measured.get("cpu_logical") or node_caps.get("cpu_count_logical") or 0)
    memory_mb = float(measured.get("memory_total_mb") or node_caps.get("memory_total_mb") or 0.0)
    gpus = measured.get("gpus") or node_caps.get("gpus") or []
    return {
        "cpu_logical": cpu_count,
        "memory_total_mb": memory_mb,
        "gpus": gpus,
    }


def _offer_runtime_image_ref(image: ImageArtifact) -> str:
    registry = (image.registry or "").strip().rstrip("/")
    base = f"{image.repository}:{image.tag}"
    return f"{registry}/{base}" if registry else base


def build_runtime_image_ref(image: ImageArtifact) -> str:
    return _offer_runtime_image_ref(image)


def get_or_create_image_offer_stub(db: Session, *, image_artifact: ImageArtifact, node: Node) -> ImageOffer:
    offer = db.scalar(
        select(ImageOffer).where(
            ImageOffer.image_artifact_id == image_artifact.id,
            ImageOffer.node_id == node.id,
        )
    )
    if offer is None:
        offer = ImageOffer(
            seller_user_id=image_artifact.seller_user_id,
            node_id=node.id,
            image_artifact_id=image_artifact.id,
            repository=image_artifact.repository,
            tag=image_artifact.tag,
            digest=image_artifact.digest,
            runtime_image_ref=_offer_runtime_image_ref(image_artifact),
            offer_status="draft",
            probe_status="pending",
        )
        db.add(offer)
        db.commit()
        db.refresh(offer)
        return offer
    offer.repository = image_artifact.repository
    offer.tag = image_artifact.tag
    offer.digest = image_artifact.digest
    offer.runtime_image_ref = _offer_runtime_image_ref(image_artifact)
    db.commit()
    db.refresh(offer)
    return offer


def price_image_offer(db: Session, offer: ImageOffer, rate_card: ResourceRateCard, now: datetime | None = None) -> ImageOffer:
    now = now or utcnow()
    node = db.get(Node, offer.node_id)
    if node is None:
        raise PricingEngineError("offer_node_missing")

    resources = _extract_offer_resources(offer, node)
    cpu_value_usd = float(resources["cpu_logical"]) * float(rate_card.cpu_price_usd_per_hour)
    ram_value_usd = (float(resources["memory_total_mb"]) / 1024.0) * float(rate_card.ram_price_usd_per_gib_hour)
    gpu_value_usd = 0.0
    gpu_components: list[dict[str, Any]] = []
    unmapped_gpu_models: list[str] = []
    gpu_rates = rate_card.gpu_price_usd_per_hour or {}

    for gpu in resources["gpus"]:
        model = str(gpu.get("model") or "").strip().lower()
        count = int(gpu.get("count") or 1)
        gpu_rate = gpu_rates.get(model)
        if gpu_rate is None:
            unmapped_gpu_models.append(model or "unknown")
            continue
        gpu_value_usd += gpu_rate * count
        gpu_components.append({"model": model, "count": count, "unit_price_usd_per_hour": gpu_rate})

    if unmapped_gpu_models:
        offer.offer_status = "pricing_blocked"
        offer.pricing_error = f"gpu_unmapped:{','.join(unmapped_gpu_models)}"
        offer.pricing_stale_at = now
        offer.last_priced_at = now
        db.commit()
        db.refresh(offer)
        return offer

    total_reference_cny = (cpu_value_usd + ram_value_usd + gpu_value_usd) * float(rate_card.usd_cny_rate)
    price_snapshot = ImageOfferPriceSnapshot(
        offer_id=offer.id,
        resource_rate_card_id=rate_card.id,
        effective_hour=truncate_to_hour(now),
        reference_price_cny_per_hour=total_reference_cny,
        billable_price_cny_per_hour=total_reference_cny,
        price_components={
            "cpu_value_usd_per_hour": cpu_value_usd,
            "ram_value_usd_per_hour": ram_value_usd,
            "gpu_value_usd_per_hour": gpu_value_usd,
            "gpus": gpu_components,
            "usd_cny_rate": rate_card.usd_cny_rate,
        },
        probe_measured_capabilities=resources,
        stale_at=rate_card.stale_at,
    )
    db.add(price_snapshot)
    db.flush()

    offer.current_reference_price_cny_per_hour = total_reference_cny
    offer.current_billable_price_cny_per_hour = total_reference_cny
    offer.current_price_snapshot_id = price_snapshot.id
    offer.last_priced_at = now
    offer.pricing_stale_at = rate_card.stale_at
    offer.pricing_error = None
    offer.offer_status = "active"
    db.commit()
    db.refresh(offer)
    return offer


def refresh_all_image_offer_prices(db: Session, now: datetime | None = None, rate_card: ResourceRateCard | None = None) -> int:
    now = now or utcnow()
    rate_card = rate_card or latest_valid_resource_rate_card(db)
    if rate_card is None:
        return 0
    offers = db.scalars(select(ImageOffer).order_by(ImageOffer.id)).all()
    count = 0
    for offer in offers:
        price_image_offer(db, offer, rate_card, now=now)
        count += 1
    return count


def ensure_current_rate_card(db: Session, now: datetime | None = None) -> ResourceRateCard | None:
    now = ensure_aware_utc(now or utcnow()) or utcnow()
    current = latest_resource_rate_card(db)
    if current is None:
        return refresh_resource_rate_card(db, now=now)
    current_effective_hour = ensure_aware_utc(current.effective_hour)
    if current_effective_hour is None:
        return refresh_resource_rate_card(db, now=now)
    age = now - current_effective_hour
    if age >= timedelta(seconds=settings.PRICING_REFRESH_INTERVAL_SECONDS):
        return refresh_resource_rate_card(db, now=now)
    return current


def publish_or_update_image_offer(
    db: Session,
    *,
    image_artifact: ImageArtifact,
    node: Node,
    probe_measured_capabilities: dict[str, Any],
    now: datetime | None = None,
) -> ImageOffer:
    now = now or utcnow()
    offer = db.scalar(
        select(ImageOffer).where(
            ImageOffer.image_artifact_id == image_artifact.id,
            ImageOffer.node_id == node.id,
        )
    )
    if offer is None:
        offer = ImageOffer(
            seller_user_id=image_artifact.seller_user_id,
            node_id=node.id,
            image_artifact_id=image_artifact.id,
            repository=image_artifact.repository,
            tag=image_artifact.tag,
            digest=image_artifact.digest,
            runtime_image_ref=_offer_runtime_image_ref(image_artifact),
            offer_status="draft",
        )
        db.add(offer)
        db.flush()

    offer.repository = image_artifact.repository
    offer.tag = image_artifact.tag
    offer.digest = image_artifact.digest
    offer.runtime_image_ref = _offer_runtime_image_ref(image_artifact)
    offer.probe_status = "completed"
    offer.probe_measured_capabilities = probe_measured_capabilities
    offer.last_probed_at = now
    db.commit()
    db.refresh(offer)
    return offer

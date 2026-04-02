from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_node_token, get_current_user
from app.api.routes import platform as platform_api
from app.api.routes.platform.common import serialize_image
from app.core.db import get_db
from app.models.identity import NodeRegistrationToken, User
from app.schemas.platform.images import ImageArtifactResponse, ImageReportRequest
from app.services.activity import log_activity
from app.services.platform_images import create_or_update_reported_image, get_seller_image, list_seller_images
from app.services.platform_nodes import get_node_for_token
from app.services.pricing_engine import PricingEngineError
from app.services.swarm_manager import SwarmManagerError

router = APIRouter()


@router.post("/images/report", response_model=ImageArtifactResponse)
def report_uploaded_image(
    payload: ImageReportRequest,
    node_token: NodeRegistrationToken = Depends(get_current_node_token),
    db: Session = Depends(get_db),
) -> ImageArtifactResponse:
    node = get_node_for_token(db, node_token, payload.node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node is not registered.")

    image = create_or_update_reported_image(db, node_token=node_token, node=node, payload=payload)
    node_token.last_used_at = node.last_heartbeat_at
    db.flush()
    log_activity(
        db,
        seller_user_id=node_token.user_id,
        node_id=node.id,
        image_id=image.id,
        event_type="image_reported",
        summary=f"Reported image {payload.repository}:{payload.tag}",
        metadata={"repository": payload.repository, "tag": payload.tag, "registry": payload.registry},
    )
    db.commit()
    db.refresh(image)

    try:
        platform_api.run_offer_probe_and_pricing(
            db,
            seller_user_id=node_token.user_id,
            image=image,
            node=node,
            timeout_seconds=platform_api.settings.PRICING_PROBE_TIMEOUT_SECONDS,
        )
    except SwarmManagerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image reported, but auto-publish failed during remote probe: {exc}",
        ) from exc
    except PricingEngineError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Image reported, but auto-publish failed during pricing: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Image reported, but auto-publish failed unexpectedly: {exc}",
        ) from exc

    return serialize_image(image)


@router.get("/images", response_model=list[ImageArtifactResponse])
def list_seller_images_route(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ImageArtifactResponse]:
    return [serialize_image(image) for image in list_seller_images(db, current_user.id)]


@router.get("/images/{image_id}", response_model=ImageArtifactResponse)
def get_seller_image_route(
    image_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImageArtifactResponse:
    image = get_seller_image(db, seller_user_id=current_user.id, image_id=image_id)
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")
    return serialize_image(image)

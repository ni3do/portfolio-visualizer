from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz", summary="Health check")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


from pydantic import BaseModel

from app.services.model_gateway import ModelHealth, ModelInfo


class ModelListResponse(BaseModel):
    default_model_id: str
    models: list[ModelInfo]
    health: ModelHealth

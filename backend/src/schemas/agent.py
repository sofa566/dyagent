
from pydantic import BaseModel


class AgentBase(BaseModel):
    name: str
    description: str | None = ''
    model_type: str = 'cloud'
    agent_class: str = 'tasked'
    enabled: bool = True


class AgentCreate(AgentBase):
    model_config: dict = {}


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    model_type: str | None = None
    agent_class: str | None = None
    enabled: bool | None = None
    model_config: dict | None = None


class AgentResponse(AgentBase):
    id: str
    is_router: bool
    model_config: dict
    workspace_id: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True

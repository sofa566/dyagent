from pydantic import BaseModel
from typing import Optional


class AgentBase(BaseModel):
    name: str
    description: Optional[str] = ''
    model_type: str = 'cloud'
    agent_class: str = 'tasked'
    enabled: bool = True


class AgentCreate(AgentBase):
    model_config: dict = {}


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    model_type: Optional[str] = None
    agent_class: Optional[str] = None
    enabled: Optional[bool] = None
    model_config: Optional[dict] = None
    mcp_config: Optional[dict] = None
    skills: Optional[list] = None
    tools: Optional[list] = None
    rag_config: Optional[dict] = None


class AgentResponse(AgentBase):
    id: str
    is_router: bool
    model_config: dict
    mcp_config: dict
    skills: list
    tools: list
    rag_config: dict
    workspace_id: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True

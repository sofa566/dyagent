import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, String

from src.models import GUID, Base


class EventPart(Base):
    __tablename__ = "event_parts"
    __table_args__ = {"extend_existing": True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey("conversations.id"), nullable=False)
    type = Column(String(50), nullable=False)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)

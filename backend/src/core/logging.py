import structlog
from structlog.processors import CallsiteParameterAdder, CallsiteParameter
import logging
from typing import Any

from src.core.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    # format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    format='%(asctime)s [%(levelname)s] %(module)s:%(funcName)s %(filename)s:%(lineno)d - %(message)s'
)

# 降噪：關閉資料庫詳細日誌與過多第三方訊息
try:
    logging.getLogger('sqlalchemy.engine').setLevel(logging.ERROR)
    logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy.dialects').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
except Exception:
    pass

# structlog.configure(
#     processors=[
#         structlog.contextvars.merge_contextvars,
#         structlog.processors.add_log_level,
#         structlog.processors.StackInfoRenderer(),
#         structlog.dev.set_exc_info,
#         structlog.processors.TimeStamper(fmt='iso'),

#         CallsiteParameterAdder([
#             CallsiteParameter.FILENAME,
#             CallsiteParameter.FUNC_NAME,
#             CallsiteParameter.LINENO,
#         ]),
#         structlog.dev.ConsoleRenderer() if settings.DEBUG else structlog.processors.JSONRenderer(),

#     ],
#     wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
#     context_class=dict,
#     logger_factory=structlog.PrintLoggerFactory(),
#     cache_logger_on_first_use=True,
# )

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        # CallsiteParameterAdder([
        #     CallsiteParameter.FILENAME,
        #     CallsiteParameter.FUNC_NAME,
        #     CallsiteParameter.LINENO,
        # ]),
        structlog.dev.ConsoleRenderer(colors=False) if settings.DEBUG else structlog.processors.JSONRenderer(),
        # structlog.stdlib.render_to_log_kwargs,
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)

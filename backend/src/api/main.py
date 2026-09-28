from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from src.api.errors import (
    general_exception_handler,
)
from src.api.routes import (
    access_control,
    admin_dashboard,
    agents,
    auth,
    chat,
    cost_usage,
    functions_admin,
    health_education,
    line_integration,
    logs,
    mcp,
    mcps_admin,
    monitoring_compliance,
    patient_auth_line,
    rag,
    renal_dashboard,
    renal_dialysis,
    renal_monitoring,
    renal_patients,
    scheduler_tasks,
    skill_ui,
    skills_admin,
    users,
)
from src.core.config import settings
from src.core.database import SessionLocal
from src.core.logging import get_logger
from src.models import *  # noqa: F403
from src.schemas.utils import first_user
from src.services.access_control_service import access_control_service
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service
from src.services.scheduler_task_service import scheduler_task_service

logger = get_logger(__name__)

logger.debug('API settings loaded')

def _seed_router_agent(db):
    # 目的：確保系統存在主代理（Router）供 /api/chat 統一入口使用。
    # 為什麼：一般使用者不應承擔手動選代理者成本，需先有預設路由代理。
    try:
        from src.models import Agent, Workspace

        router = db.query(Agent).filter(Agent.is_router == True).first()  # noqa: E712
        if router is not None:
            changed = False
            if str(getattr(router, 'agent_class', '') or '') != 'master':
                router.agent_class = 'master'
                changed = True
            if not bool(getattr(router, 'enabled', True)):
                router.enabled = True
                changed = True
            if changed:
                db.commit()
            return

        ws = db.query(Workspace).first()
        if ws is None:
            ws = Workspace(name='default')
            db.add(ws)
            db.commit()
            db.refresh(ws)

        router = Agent(
            name='主代理',
            description='負責將使用者問題分派到合適代理者',
            system_prompt='你是主代理，負責判斷問題並選擇合適的部門代理者。',
            model_type='cloud',
            agent_class='master',
            enabled=True,
            is_router=True,
            model_config={'tier': 'cloud'},
            workspace_id=ws.id,
        )
        db.add(router)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning('router.seed_failed', error=str(e))


def _ensure_weather_skill_open_meteo(db):
    """將既有「天氣查詢」技能切換為 Open-Meteo Python handler。"""
    try:
        from src.models import SkillEntry

        row = db.query(SkillEntry).filter(SkillEntry.name == '天氣查詢').first()
        if row is None:
            row = SkillEntry(
                name='天氣查詢',
                description='使用 Open-Meteo 查詢城市即時天氣',
                enabled=True,
                type='python',
                python_handler='src.skills.open_meteo:get_weather',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'q': {'type': 'string', 'description': '城市名稱，例如 台北'},
                    },
                    'required': ['q'],
                    'additionalProperties': True,
                },
            )
            db.add(row)
            db.commit()
            return

        changed = False
        if str(getattr(row, 'type', '') or '') != 'python':
            row.type = 'python'
            changed = True
        if str(getattr(row, 'python_handler', '') or '') != 'src.skills.open_meteo:get_weather':
            row.python_handler = 'src.skills.open_meteo:get_weather'
            changed = True
        # 轉為 python handler 後，不再依賴 webhook URL
        if getattr(row, 'endpoint_url', None):
            row.endpoint_url = None
            changed = True
        if str(getattr(row, 'http_method', 'POST') or 'POST').upper() != 'POST':
            row.http_method = 'POST'
            changed = True
        if not bool(getattr(row, 'enabled', True)):
            row.enabled = True
            changed = True
        if changed:
            db.commit()
    except Exception as e:
        logger.warning('weather_skill.ensure_failed', error=str(e))


def _ensure_renal_companion_agent(db):
    # 目的：確保系統存在「腎友陪伴」代理供 LINE 腎友會話固定使用。
    # 為什麼：若缺少專屬代理，LINE 會話可能回退一般代理導致流程混雜。
    try:
        from src.models import Agent, Workspace

        configured_agent_name = str(getattr(settings, 'LINE_RENAL_COMPANION_AGENT_NAME', '腎友陪伴') or '腎友陪伴').strip()
        if not configured_agent_name:
            return
        renal_companion_description = '專責處理腎友 LINE 對話、每日回報提醒與陪伴回覆'
        renal_companion_prompt = (
            '你是「腎友陪伴」代理，僅處理腎友照護 LINE 對話，不做一般聊天分流。\n'
            '你的目標是讓病患完成每日回報，並以溫和、清楚、可執行的語句引導。\n\n'
            '【固定流程】\n'
            '1) 先判斷時段：早晨（MORNING）或晚間（EVENING）。若使用者未說明，先詢問目前要回報早晨或晚間。\n'
            '2) 收集必填欄位：\n'
            '   - 血壓：收縮壓/舒張壓（例如 128/76）\n'
            '   - 體重（kg）\n'
            '3) 若病患為糖尿病（is_diabetic=true），必須再收集血糖（blood_glucose_mg_dl）。\n'
            '4) 若欄位缺漏，逐一補問，不可跳過；補齊後再確認一次。\n'
            '5) 完成時回覆「已收到」與下一步提醒（例如晚間仍需再回報一次）。\n\n'
            '【語氣與邊界】\n'
            '- 保持關懷與鼓勵，使用短句，不使用責備語氣。\n'
            '- 不提供診斷、不下醫囑、不替代醫療判斷。\n'
            '- 若使用者提到危急症狀（胸痛、呼吸困難、意識改變、持續出血、跌倒後異常），立即建議就醫並通知護理師。\n\n'
            '【輸出格式建議】\n'
            '- 優先用條列或逐步提問，避免一次丟太多問題。\n'
            '- 若收到數值，先重述確認，再進入下一題。\n'
            '- 若資料格式不清楚，請求重新輸入範例（例如：血壓 128/76、體重 63.4、血糖 142）。'
        )

        existing = db.query(Agent).filter(Agent.name == configured_agent_name).first()
        if existing is not None:
            changed = False
            if not bool(getattr(existing, 'enabled', True)):
                existing.enabled = True
                changed = True
            if str(getattr(existing, 'description', '') or '').strip() != renal_companion_description:
                existing.description = renal_companion_description
                changed = True
            if str(getattr(existing, 'system_prompt', '') or '').strip() != renal_companion_prompt:
                existing.system_prompt = renal_companion_prompt
                changed = True
            if changed:
                db.commit()
            return

        ws = db.query(Workspace).first()
        if ws is None:
            ws = Workspace(name='default')
            db.add(ws)
            db.commit()
            db.refresh(ws)

        renal_companion_agent = Agent(
            name=configured_agent_name,
            description=renal_companion_description,
            system_prompt=renal_companion_prompt,
            model_type='cloud',
            agent_class='tasked',
            enabled=True,
            is_router=False,
            model_config={'tier': 'cloud'},
            workspace_id=ws.id,
        )
        db.add(renal_companion_agent)
        db.commit()
    except Exception as error:
        logger.warning('renal_companion.seed_failed', error=str(error))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('application_startup', version=settings.VERSION)
    await redis_service.connect()
    qdrant_service.connect()
    logger.info('database_migrations_expected', mode='alembic_only')

    db: Session = SessionLocal()
    try:
        message = first_user(db)
        print(message)
        access_control_service.ensure_system_roles(db)
        _ensure_weather_skill_open_meteo(db)
        _seed_router_agent(db)
        _ensure_renal_companion_agent(db)
        scheduler_task_service.ensure_default_renal_reminder_tasks(db=db)
    finally:
        db.close()
    yield
    logger.info('application_shutdown')
    await redis_service.disconnect()
    qdrant_service.disconnect()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.add_exception_handler(Exception, general_exception_handler)

app.include_router(auth.router, prefix='/api', tags=['認證'])
app.include_router(agents.router, prefix='/api', tags=['代理者'])
app.include_router(chat.router, prefix='/api', tags=['聊天'])
app.include_router(mcp.router, prefix='/api', tags=['MCP'])
app.include_router(rag.router, prefix='/api', tags=['RAG'])
app.include_router(users.router, prefix='/api', tags=['使用者'])
app.include_router(access_control.router, prefix='/api', tags=['權限管理'])
app.include_router(logs.router, prefix='/api', tags=['日誌'])
app.include_router(admin_dashboard.router, prefix='/api', tags=['管理儀表板'])
app.include_router(cost_usage.router, prefix='/api', tags=['成本治理'])
app.include_router(line_integration.router, prefix='/api', tags=['LINE 通道'])
app.include_router(patient_auth_line.router, prefix='/api', tags=['腎友入口'])
app.include_router(renal_monitoring.router, prefix='/api', tags=['腎友日常監測'])
app.include_router(monitoring_compliance.router, prefix='/api', tags=['腎友回報完整性'])
app.include_router(health_education.router, prefix='/api', tags=['衛教內容'])
app.include_router(renal_dialysis.router, prefix='/api', tags=['透析療程'])
app.include_router(renal_dashboard.router, prefix='/api', tags=['腎友照護儀表板'])
app.include_router(renal_patients.router, prefix='/api', tags=['腎友管理'])
app.include_router(scheduler_tasks.router, prefix='/api', tags=['排程任務'])
app.include_router(mcps_admin.router, prefix='/api', tags=['MCP 管理'])
app.include_router(skills_admin.router, prefix='/api', tags=['技能管理'])
app.include_router(functions_admin.router, prefix='/api', tags=['Functions 管理'])
app.include_router(skill_ui.router, prefix='/api', tags=['技能 UI'])


@app.get('/')
async def root():
    return {'message': 'Welcome to dyagent API', 'version': settings.VERSION}


@app.get('/health')
async def health():
    return {'status': 'healthy'}

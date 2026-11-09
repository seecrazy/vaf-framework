"""e2e 共享装置：构建带 Mock LLM 的示例领域应用（fixtures 定义见 conftest.py）。"""
from vaf.app import VafApp
from vaf.demo_llm import DemoResponder
from vaf.llm.mock import ScriptedMockLLM


def make_app():
    app = VafApp()
    app.register_model_endpoint(
        "mock-main", ScriptedMockLLM(name="mock-main", responder=DemoResponder()), tier=1
    )
    import medical_triage
    import ops_incident

    medical_triage.register(app)
    ops_incident.register(app)
    return app


EMERGENCY_PATIENT = {
    "raw_id": "p-em", "source": "hotline", "timestamp": "2026-11-08T10:00:00",
    "chief_complaint": "胸痛 3 小时，伴出汗",
    "vitals": {"heart_rate": 128, "temperature_c": 36.8, "systolic_bp": 150},
    "age": 58, "gender": "male",
}

MILD_PATIENT = {
    "raw_id": "p-mild", "source": "hotline", "timestamp": "2026-11-08T10:00:00",
    "chief_complaint": "轻微咳嗽 2 天",
    "vitals": {"heart_rate": 76, "temperature_c": 36.5, "systolic_bp": 120},
    "age": 30, "gender": "female",
}

INCIDENT_INPUT = {
    "raw_id": "inc-1", "source": "prometheus", "timestamp": "2026-11-08T10:01:00",
    "service": "payment", "oncall": "alice",
    "alerts": [
        {"id": "a1", "alert_key": "K1", "severity": "critical", "title": "db-proxy conn pool"},
        {"id": "a2", "alert_key": "K1", "severity": "critical", "title": "db-proxy conn pool"},
        {"id": "a3", "alert_key": "K2", "severity": "", "title": "no severity"},
    ],
}

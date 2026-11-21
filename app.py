import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st

from financial_research.domain import Clarification
from financial_research.fixtures import EXAMPLES, PROMPTS, PROMPTS_ZH
from financial_research.presentation import workspace_view
from financial_research.service import ResearchService

ASSETS = Path(__file__).with_name("financial_research") / "ui"

st.set_page_config(page_title="Financial Research Agent", layout="wide")
st.html("""
<style>
[data-testid="stHeader"], #MainMenu { display: none; }
[data-testid="stMainBlockContainer"] { max-width: none; padding: 0; }
</style>
""")


@st.cache_resource
def runtime():
    service = ResearchService(db=os.environ.get("FINANCIAL_RESEARCH_DB"), recover=True)
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="investigation")
    if not service.store.selection():
        service.store.new_session()
    return service, worker


@st.cache_resource
def register_component(css, javascript):
    return st.components.v2.component(
        "research_workspace", html='<div class="research-app"></div>', css=css, js=javascript
    )


service, worker = runtime()
component = register_component(
    (ASSETS / "style.css").read_text(encoding="utf-8"),
    "\n".join((ASSETS / name).read_text(encoding="utf-8") for name in ("locales.js", "workspace.js")),
)


def active_settings():
    settings = service.settings
    override = st.session_state.get("model_override")
    return settings.with_model(override) if override is not None else settings


def submit(turn, answers=None, *, settings):
    return asyncio.run(service.execute(turn, answers, settings=settings))


def handle(event, selection, selected, settings):
    command = event.get("command")
    if command == "save_model_settings":
        st.session_state.model_override = settings.model_from_form(event)
        st.session_state.model_settings_feedback = {
            "error": False,
            "message": "Model settings applied for this session.",
        }
    elif command == "reset_model_settings":
        st.session_state.pop("model_override", None)
        st.session_state.model_settings_feedback = {
            "error": False,
            "message": "Restored settings from the configuration file and environment.",
        }
    elif command == "mode" and event.get("mode") in {"replay", "live"}:
        st.session_state.mode = event["mode"]
    elif command == "new":
        service.store.new_session()
    elif command == "delete_session" and event.get("id") == selection["session_id"]:
        service.delete_session(selection["session_id"])
    elif command == "session":
        session = event.get("id")
        if session not in {item["id"] for item in service.store.sessions()}:
            raise ValueError("Select an existing session.")
        turns = service.store.turns(session)
        service.store.select(session, turns[-1]["id"] if turns else None)
    elif command == "turn":
        service.store.select(selection["session_id"], event.get("id"))
    elif command == "run":
        mode = st.session_state.mode
        if mode == "live" and not settings.model_ready:
            raise ValueError(settings.model_error + " You can also choose Fixture replay.")
        parent = (
            selected["id"]
            if selected and selected["status"] == "Complete" and selected["mode"] == mode
            else None
        )
        turn = service.start(selection["session_id"], event.get("question"), mode, parent, settings=settings)
        st.session_state.worker_future = worker.submit(submit, turn, settings=settings)
    elif command == "clarify":
        if not selected or selected["status"] != "Waiting for clarification":
            raise ValueError("Open the pending clarification before answering.")
        if selected["mode"] == "live" and not settings.model_ready:
            raise ValueError(settings.model_error)
        answers = Clarification.model_validate(selected["clarification"]).validate_answers(
            event.get("answers") or {}
        )
        st.session_state.worker_future = worker.submit(submit, selected["id"], answers, settings=settings)


if "mode" not in st.session_state:
    st.session_state.mode = "live" if active_settings().model_ready else "replay"


def in_progress(turn):
    future = st.session_state.get("worker_future")
    return bool(turn and turn["status"] in {"Queued", "Running"} or future and not future.done())


active_turn = service.store.selection()["turn_id"]
polling = in_progress(service.store.turn(active_turn) if active_turn else None)


@st.fragment(run_every=0.35 if polling else None)
def research_workspace():
    selection = service.store.selection()
    selected = service.view(selection["turn_id"]) if selection["turn_id"] else None
    if polling and not in_progress(selected):
        st.rerun()
    settings = active_settings()
    result = component(
        key="research-workspace",
        on_action_change=lambda: None,
        data={
            "mode": st.session_state.mode,
            "selection": selection,
            "sessions": service.store.sessions(),
            "turns": service.store.turns(selection["session_id"]),
            "selected": workspace_view(selected),
            "messages": list(settings.messages),
            "modelSettings": {
                **settings.model_settings_view(),
                "overridden": "model_override" in st.session_state,
            },
            "modelSettingsFeedback": st.session_state.get("model_settings_feedback"),
            "examples": [
                {"label": label, "questions": {"en": PROMPTS[key], "zh": PROMPTS_ZH[key]}}
                for label, key in EXAMPLES.items()
            ],
            "error": st.session_state.get("workspace_error", ""),
            "acknowledged": st.session_state.get("last_nonce"),
        },
    )
    event = result.get("action")
    if not isinstance(event, dict) or event.get("nonce") == st.session_state.get("last_nonce"):
        return
    st.session_state.last_nonce = event.get("nonce")
    st.session_state.workspace_error = ""
    try:
        handle(event, selection, selected, settings)
    except ValueError as exc:
        message = settings.redact(str(exc))
        if event.get("command") in ("save_model_settings", "reset_model_settings"):
            st.session_state.model_settings_feedback = {"error": True, "message": message}
        else:
            st.session_state.workspace_error = message
    st.rerun()


research_workspace()

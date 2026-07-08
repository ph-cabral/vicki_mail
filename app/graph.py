from langgraph.graph import END, StateGraph

from app.graph_state import EmailState
from app.nodes import (
    analyze_cv_node,
    check_attachments,
    delete_and_notice_node,
    error_node,
    extract_text_node,
    ignore_node,
    match_candidato_node,
    meeting_notes_node,
    persist_cv_node,
    reply_existente_node,
    reply_imagen_node,
    reply_nuevo_node,
    reply_sin_cv_node,
    router_email,
)


def _route_inicial(state: EmailState) -> str:
    return state["route"]  # interno | ignorar | readai | fireflies | candidato


def _route_adjunto(state: EmailState) -> str:
    return state["route"]  # sin_cv | con_cv


def _route_texto(state: EmailState) -> str:
    return state["route"]  # imagen | texto_ok


def _route_analisis(state: EmailState) -> str:
    return state["route"]  # error_llm | ok


def _route_match(state: EmailState) -> str:
    return state["route"]  # nuevo | existente


def build_graph() -> StateGraph:
    builder = StateGraph(EmailState)

    builder.add_node("router", router_email)
    builder.add_node("check_attachments", check_attachments)
    builder.add_node("extract_text", extract_text_node)
    builder.add_node("analyze_cv", analyze_cv_node)
    builder.add_node("match_candidato", match_candidato_node)
    builder.add_node("persist_cv", persist_cv_node)
    builder.add_node("reply_nuevo", reply_nuevo_node)
    builder.add_node("reply_existente", reply_existente_node)
    builder.add_node("reply_imagen", reply_imagen_node)
    builder.add_node("reply_sin_cv", reply_sin_cv_node)
    builder.add_node("delete_and_notice", delete_and_notice_node)
    builder.add_node("meeting_notes", meeting_notes_node)
    builder.add_node("ignore", ignore_node)
    builder.add_node("error", error_node)

    builder.set_entry_point("router")

    builder.add_conditional_edges("router", _route_inicial, {
        "interno": "delete_and_notice",
        "ignorar": "ignore",
        "readai": "meeting_notes",
        "fireflies": "meeting_notes",
        "candidato": "check_attachments",
    })

    builder.add_conditional_edges("check_attachments", _route_adjunto, {
        "sin_cv": "reply_sin_cv",
        "con_cv": "extract_text",
    })

    builder.add_conditional_edges("extract_text", _route_texto, {
        "imagen": "reply_imagen",
        "texto_ok": "analyze_cv",
    })

    builder.add_conditional_edges("analyze_cv", _route_analisis, {
        "error_llm": "error",
        "ok": "match_candidato",
    })

    builder.add_edge("match_candidato", "persist_cv")

    builder.add_conditional_edges("persist_cv", lambda s: s["candidato"]["accion"], {
        "inserted": "reply_nuevo",
        "updated": "reply_existente",
    })

    for terminal in (
        "reply_nuevo", "reply_existente", "reply_imagen", "reply_sin_cv",
        "delete_and_notice", "meeting_notes", "ignore", "error",
    ):
        builder.add_edge(terminal, END)

    return builder

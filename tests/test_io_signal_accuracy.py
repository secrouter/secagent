"""IO-signal accuracy regressions (from the cFS docs review).

- runtime-wiring signals are not mined from prose (a README that *mentions* PyZMQ);
- the generic message-bus pattern no longer matches the ``PubSub`` substring inside
  CamelCase type names (cFS's ``..._PubSub_t``);
- the cFE Software Bus is detected from real ``CFE_SB_*`` calls;
- the Express/Koa endpoint pattern requires a leading ``/`` (so a Ruby
  ``name.delete("CFS-")`` string op is not mistaken for an HTTP route).
"""

from __future__ import annotations

from secagent.affordances.file_summary import summarize_file
from secagent.affordances.signals import find_endpoints, find_messaging


def test_readme_pyzmq_mention_is_not_messaging():
    md = "# cFS\n\nThe ground system depends on PyQt5 and PyZMQ.\n"
    summary = summarize_file("README.md", md, "Markdown", [])
    assert summary.messaging == []  # prose mention must not produce a ZeroMQ edge


def test_structured_config_messaging_still_detected():
    # Config is not prose — real signals are kept (e.g. compose referencing Kafka).
    yml = "services:\n  bus:\n    image: confluentinc/cp-kafka\n"
    summary = summarize_file("docker-compose.yml", yml, "YAML", [])
    assert "Kafka" in summary.messaging


def test_pubsub_type_name_is_not_generic_bus():
    code = "typedef struct { int x; } EdsInterface_CFE_SB_SoftwareBus_PubSub_t;\n"
    assert "Message bus (generic)" not in find_messaging(code)


def test_cfe_software_bus_detected_from_real_calls():
    code = (
        "CFE_SB_Subscribe(FM_CMD_MID, FM_AppData.CmdPipe);\n"
        "CFE_SB_TransmitMsg(&Pkt.TlmHeader.Msg, true);\n"
    )
    assert find_messaging(code) == ["cFE Software Bus"]


def test_ruby_delete_string_is_not_an_endpoint():
    rb = 'def cpu(target_name)\n  Integer(target_name.delete("CFS-") || "")\nend\n'
    assert find_endpoints(rb) == []


def test_real_express_routes_still_detected():
    js = 'app.get("/api/users", handler);\nrouter.post("/login", h);\n'
    eps = find_endpoints(js)
    assert "/api/users" in eps and "/login" in eps

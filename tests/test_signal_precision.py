"""Precision tightening for weak signal anchors and JS/TS symbol coverage.
Regression coverage for the Tier-3 review items."""

from __future__ import annotations

from secagent.affordances.signals import find_sockets
from secagent.affordances.symbols import extract_symbols


def test_udp_anchor_requires_call_shape():
    # A real UDP call is detected...
    assert find_sockets("n = recvfrom(fd, buf, len, 0);") == ["UDP socket"]
    assert find_sockets("sendto(fd, buf, len, 0, &addr, alen);") == ["UDP socket"]
    # ...but a bare identifier / field named sendto is not.
    assert find_sockets("this.sendto = queue;  // a field, not a syscall") == []
    assert find_sockets("int sendto_count = 0;") == []


def test_generic_socket_anchor_requires_call_shape():
    assert find_sockets("getaddrinfo(host, port, &hints, &res);") == ["Socket (generic)"]
    assert find_sockets("setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, 4);") == \
        ["Socket (generic)"]
    # Bare mentions (docs, identifiers) no longer classify a socket.
    assert find_sockets("// see getaddrinfo notes; setsockopt tuning") == []
    assert find_sockets("var getaddrinfo_helper = 1;") == []


def test_js_arrow_let_var_and_export_default():
    src = (
        "export default function App() {}\n"
        "let handler = (req) => {}\n"
        "var make = async () => {}\n"
        "export default class Widget {}\n"
    )
    names = {s.name for s in extract_symbols("a.js", src, "JavaScript")}
    assert {"App", "handler", "make", "Widget"} <= names


def test_ts_class_methods_captured_not_control_flow():
    src = (
        "class Svc {\n"
        "  async load(id: number): Promise<X> {\n"
        "    if (id) { return x; }\n"
        "    for (const y of z) { work(y); }\n"
        "  }\n"
        "  public name(): string { return this._n; }\n"
        "}\n"
    )
    syms = {(s.kind, s.name) for s in extract_symbols("s.ts", src, "TypeScript")}
    names = {n for _, n in syms}
    assert "load" in names and "name" in names
    # Control-flow keywords must not be captured as methods.
    for kw in ("if", "for", "return", "while", "switch"):
        assert kw not in names


def test_aws_messaging_requires_real_aws_context():
    """Both run-3 devs hit this: secagent reported an AWS SQS/SNS dependency for an offline
    embedded UART parser with no networking at all. The cause was a bare three-letter
    token — `SNS` inside the Ashtech GPS command string "$PASHS,SNS,DUO,2"."""
    from secagent.affordances.signals import find_messaging

    for text in ('const char duo_mode[] = "$PASHS,SNS,DUO,2\\r\\n";',
                 "// SNS handling", "int sqs = 3;", "snsr_read()"):
        assert find_messaging(text) == [], text
    for text in ('sqs_client = boto3.client("sqs")', "from aws_sqs import Queue",
                 "AmazonSQSClient c;", "new SnsClient()"):
        assert "AWS SQS/SNS" in find_messaging(text), text

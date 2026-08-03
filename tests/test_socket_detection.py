"""Raw socket usage detection in the IO map (TCP/UDP/Unix).

cFS does a lot of socket networking (SBN, ci_lab/to_lab UDP, OSAL's OS_Socket*) that the
broker-oriented messaging detector misses. find_sockets classifies transport where the
evidence is clear and reports a generic socket otherwise; the signal is prose-gated and
flows through the IO map as a ``socket`` edge.
"""

from __future__ import annotations

from secagent.affordances.file_summary import summarize_file
from secagent.affordances.io_map import build_io_map
from secagent.affordances.models import FileRecord
from secagent.affordances.signals import find_sockets


def test_tcp_socket_classified():
    assert find_sockets("fd = socket(AF_INET, SOCK_STREAM, 0);") == ["TCP socket"]


def test_udp_socket_classified():
    assert find_sockets("fd = socket(AF_INET, SOCK_DGRAM, 0);") == ["UDP socket"]


def test_cfs_osal_socket_detected():
    code = "OS_SocketOpen(&id, OS_SocketDomain_INET, OS_SocketType_DATAGRAM);"
    assert find_sockets(code) == ["UDP socket"]


def test_generic_socket_when_transport_unknown():
    code = "import socket\ns = socket.socket(socket.AF_INET)"
    assert find_sockets(code) == ["Socket (generic)"]  # generic, no transport evidence


def test_no_socket_no_match():
    assert find_sockets("int add(int a, int b){ return a + b; }") == []


def test_socket_signal_gated_from_prose():
    md = "# App\n\nIt opens a TCP socket (SOCK_STREAM) to the ground station.\n"
    assert summarize_file("README.md", md, "Markdown", []).sockets == []


def test_socket_produces_io_edge():
    recs = [FileRecord(path="sbn/sbn_udp.c", language="C", size=10, sha256="x", loc=5)]
    summ = summarize_file("sbn/sbn_udp.c", "socket(AF_INET, SOCK_DGRAM, 0);", "C", [])
    _comps, edges = build_io_map(recs, {"sbn/sbn_udp.c": summ})
    socket_edges = [(e.src, e.dst) for e in edges if e.kind == "socket"]
    assert socket_edges == [("sbn", "UDP socket")]

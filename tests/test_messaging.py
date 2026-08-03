"""Message-queue / messaging detection and its surfacing on the Data Flow & IO page."""

from __future__ import annotations

from secagent.affordances import signals
from secagent.affordances.io_map import build_io_map, summarize_io
from secagent.affordances.models import FileRecord, FileSummary, IOEdge
from secagent.agents.docs.outline import _dataflow_page


def test_find_messaging_detects_brokers_and_libraries():
    assert "Kafka" in signals.find_messaging("from confluent_kafka import Producer")
    assert "MQTT" in signals.find_messaging("import paho.mqtt.client as mqtt")
    assert "ZeroMQ" in signals.find_messaging("import zmq")
    assert "RabbitMQ/AMQP" in signals.find_messaging("channel = pika.BlockingConnection()")
    assert "NATS" in signals.find_messaging("nc = await nats.connect('demo')")
    assert "GCP Pub/Sub" in signals.find_messaging("from google.cloud.pubsub import x")
    assert "Message bus (generic)" in signals.find_messaging("class EventBus: ...")
    assert signals.find_messaging("import sqlite3\nx = 1") == []


def test_brokers_moved_out_of_datastores():
    # Kafka/RabbitMQ are messaging, not datastores; Redis stays a datastore.
    assert signals.find_datastores("import confluent_kafka") == []
    assert signals.find_messaging("import confluent_kafka") == ["Kafka"]
    assert "Redis" in signals.find_datastores("import redis")


def test_io_map_emits_messaging_edges():
    records = [FileRecord("ingest/worker.py", "Python", 100, "sha", 10, 1)]
    summaries = {"ingest/worker.py": FileSummary(path="ingest/worker.py",
                                                 messaging=["Kafka", "MQTT"])}
    _, edges = build_io_map(records, summaries)
    msg = {e.dst for e in edges if e.kind == "messaging"}
    assert msg == {"Kafka", "MQTT"}
    text = summarize_io(edges)
    assert "messaging (" in text and "Kafka" in text


def test_dataflow_page_has_messaging_section():
    edges = [
        IOEdge("ingest", "Kafka", "messaging", detail="Kafka"),
        IOEdge("worker", "Kafka", "messaging", detail="Kafka"),
        IOEdge("sensors", "MQTT", "messaging", detail="MQTT"),
    ]
    body = _dataflow_page(edges).body
    assert "Messaging" in body
    assert "**Kafka**" in body and "**MQTT**" in body
    assert "``ingest``" in body and "``worker``" in body

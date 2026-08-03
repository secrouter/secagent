"""A naming convention is not a dependency.

Infrastructure repos ship an Azure/AWS resource-naming dictionary as boilerplate:

    { "cacheRedis": "redis-", "hdInsightClustersKafka": "kafka-" }

That says how to name a cache *if you ever create one*. secagent read it as usage and
emitted two architecture edges — `infra -> Redis`, `infra -> Kafka` — for an eShop
checkout containing no Redis and no Kafka anywhere: no package reference, no bicep
resource, no compose service. The edges flowed into the generated Sphinx docs
("Kafka — used by infra") and drew a real Redis node in the architecture diagram, which
is where a fabrication does the most damage: those docs claim to be accurate by
construction, and a reader has no way to tell an invented edge from a real one.

The discriminator is narrow on purpose. A first attempt — "a file naming many different
technologies is a catalogue, not a consumer" — was measured and DISPROVED: the dictionary
yields exactly one datastore and one broker, the same shape as a real client. What
actually distinguishes it is that the matched text is a bare prefix fragment ending in a
separator. No genuine reference looks like that.
"""

from __future__ import annotations

from secagent.affordances.signals import find_datastores, find_messaging

ABBREVIATIONS = """{
    "appConfigurationConfigurationStores": "appcs-",
    "cacheRedis": "redis-",
    "documentDBDatabaseAccounts": "cosmos-",
    "hdInsightClustersKafka": "kafka-",
    "sqlServers": "sql-",
    "storageStorageAccounts": "st"
}"""


def test_naming_dictionary_creates_no_dependencies():
    assert find_datastores(ABBREVIATIONS) == []
    assert find_messaging(ABBREVIATIONS) == []


# --- everything that must still be detected -----------------------------------

def test_client_library_import_is_still_usage():
    assert "Redis" in find_datastores("using StackExchange.Redis;")
    assert "Kafka" in find_messaging("from aiokafka import KafkaProducer")


def test_container_image_tag_is_still_usage():
    assert "Redis" in find_datastores("image: redis:7-alpine")


def test_connection_url_is_still_usage():
    assert "Redis" in find_datastores("REDIS_URL=redis://cache:6379")


def test_a_bare_config_value_is_still_usage():
    """`"broker": "kafka"` has no trailing separator, so it is a real reference."""
    assert "Kafka" in find_messaging('{"broker": "kafka"}')
    assert "Redis" in find_datastores('{"provider": "redis"}')


def test_a_hyphenated_real_name_is_not_swallowed():
    """The filter must not eat a genuine hyphenated identifier that merely contains a
    technology name — only a value that IS the prefix."""
    assert "Redis" in find_datastores('{"host": "redis-primary.internal"}')
    assert "Kafka" in find_messaging('{"servers": "kafka-broker-1:9092"}')

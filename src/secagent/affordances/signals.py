"""Heuristic detectors for IO signals in source text.

These language-agnostic regexes surface the facts that matter for an architecture
view and for a reviewer's cross-component reasoning: what HTTP endpoints a file
exposes, what it calls out to, which environment variables it reads, and which
datastores/brokers it touches. They are intentionally broad — recall over precision.
"""

from __future__ import annotations

import re

# HTTP endpoints this file exposes (server side).
_ENDPOINT_RES = [
    # Flask/FastAPI/Starlette: @app.route("/x"), @router.get("/x"), @app.post(...)
    re.compile(r'@\w+\.(?:route|get|post|put|patch|delete|websocket)\(\s*["\']([^"\']+)["\']'),
    # Express/Koa: app.get("/x", ...), router.post('/x', ...). Require a leading "/" so
    # this broad `<obj>.<verb>(...)` shape doesn't match unrelated method calls on quoted
    # strings (e.g. Ruby `name.delete("CFS-")`), which aren't routes.
    re.compile(r'\b\w+\.(?:get|post|put|patch|delete|use)\(\s*["\'](/[^"\']*)["\']'),
    # Go net/http: http.HandleFunc("/x", ...), mux.Handle("/x", ...)
    re.compile(r'\.(?:HandleFunc|Handle)\(\s*"([^"]+)"'),
    # Spring: @GetMapping("/x"), @RequestMapping("/x")
    re.compile(r'@(?:Get|Post|Put|Delete|Request)Mapping\(\s*["\']([^"\']+)["\']'),
    # ASP.NET attribute routing: [HttpGet("/x")], [Route("x")]
    re.compile(r'\[(?:HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete|Route)\(\s*["\']([^"\']+)["\']'),
    # ASP.NET minimal API: app.MapGet("/x", ...), MapPost(...), MapGroup("/x")
    re.compile(r'\.Map(?:Get|Post|Put|Patch|Delete|Group)\(\s*["\']([^"\']+)["\']'),
    # Rust actix/rocket attribute routes: #[get("/x")], #[post("/x")], #[route("/x", …)]
    re.compile(r'#\[(?:get|post|put|patch|delete|head|route)\(\s*"([^"]+)"'),
    # Rust axum/warp: .route("/x", get(handler)) — require a leading "/".
    re.compile(r'\.route\(\s*"(/[^"]*)"'),
]

# Outbound calls / consumed services. Only URLs that begin a *string literal* count —
# real request targets are quoted (`requests.get("https://h/..")`), whereas license and
# documentation URLs live bare in comments, so this skips them.
_CALL_URL_RE = re.compile(r"""["'`]https?://([A-Za-z0-9._\-]+(?::\d+)?)""")
# Evidence the file actually drives an HTTP client (vs. a URL that merely appears in a
# license header, doc comment, or config). Covers Python/JS/Go, C/C++ (libcurl), and
# .NET. For .NET we require a real call/instantiation — `new HttpClient` or an async
# request method — NOT a bare `HttpClient` type in a field/parameter declaration.
_HTTP_CLIENT_RE = re.compile(
    r"\b(requests\.(?:get|post|put|patch|delete)"
    r"|httpx\.(?:get|post|put|patch|delete|Client|AsyncClient)"
    r"|urllib\.request|urlopen|fetch\(|axios\.|http\.(?:Get|Post|NewRequest)"
    r"|curl_easy_perform|CURLOPT_URL|WinHttp(?:Open|Connect)"
    r"|new\s+HttpClient"
    r"|\.(?:GetAsync|PostAsync|PutAsync|PatchAsync|DeleteAsync|SendAsync"
    r"|GetStringAsync|GetByteArrayAsync|GetStreamAsync)\("
    # Rust HTTP clients: reqwest, hyper, ureq.
    r"|reqwest::(?:get|Client|blocking)|hyper::Client|\bureq::(?:get|post|request))"
)
# Lines where an http(s) URL is a namespace / schema / serialization identifier rather
# than a request target. Skipped wholesale — XML/SOAP/data-contract namespaces are the
# dominant source of stray "outbound" hosts (especially in C#/.NET and Java).
_NS_CONTEXT_RE = re.compile(
    r"(?:\b(?:xmlns|targetNamespace|schemaLocation|namespaceURI|XmlRoot|XmlType"
    r"|XmlElement|XmlAttribute|XmlSerializer|DataContract|WebServiceBinding"
    r"|SoapType|SoapDocumentMethod|WebService)\b|\bNamespace\s*=)",
    re.I,
)
# Hosts that appear in license headers, standards/namespace URLs, and docs — never
# outbound calls even when an HTTP client is present. SCM hosts (github.com, gitlab.com)
# are deliberately NOT here: those are legitimate API targets, gated by the client check.
_NONCALL_HOSTS = (
    "apache.org", "gnu.org", "fsf.org", "opensource.org", "creativecommons.org",
    "mozilla.org", "w3.org", "xml.org", "json-schema.org", "schema.org",
    "oasis-open.org", "example.com", "example.org", "example.net",
    # XML/SOAP/serialization namespace hosts (not call targets).
    "tempuri.org", "xmlsoap.org", "openxmlformats.org", "purl.org", "iana.org",
    "dublincore.org", "docbook.org",
)

# Environment variables.
_ENV_RES = [
    re.compile(r'os\.environ(?:\.get)?\(\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'os\.environ\[\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'os\.getenv\(\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'process\.env\.([A-Z0-9_]+)'),
    re.compile(r'getenv\(\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'System\.getenv\(\s*["\']([A-Z0-9_]+)["\']'),
    # .NET: Environment.GetEnvironmentVariable("X")
    re.compile(r'Environment\.GetEnvironmentVariable\(\s*["\']([A-Za-z0-9_]+)["\']'),
    # Rust: std::env::var("X") / env::var_os("X")
    re.compile(r'\benv::var(?:_os)?\(\s*"([A-Z0-9_]+)"'),
]

# .NET IConfiguration access — Configuration["Key"] / builder.Configuration["Key:Sub"].
# Applied only when the file actually uses IConfiguration (see _CONFIG_CONTEXT_RE),
# because `\bConfiguration\[` alone also matches any ordinary member named
# ``Configuration`` being indexed (e.g. a DTO's ``record.Configuration["theme"]``).
_CONFIG_INDEXER_RE = re.compile(r'\bConfiguration\[\s*["\']([A-Za-z0-9_:.\-]+)["\']')
_CONFIG_CONTEXT_RE = re.compile(
    r"\b(?:IConfiguration|ConfigurationBuilder|AddJsonFile|builder\.Configuration"
    r"|Microsoft\.Extensions\.Configuration)\b")

# Datastores, keyed by canonical name -> matching pattern. Message brokers/queues live
# in _MESSAGING_RES below so the architecture view can call them out separately.
_DATASTORE_RES = {
    "PostgreSQL": re.compile(
        r"\b(psycopg2?|asyncpg|postgres(?:ql)?|npgsql|tokio_postgres)\b", re.I),
    "MySQL": re.compile(r"\b(pymysql|mysqlclient|mysql\.connector|mysqlconnector)\b", re.I),
    "SQLite": re.compile(r"\b(sqlite3|sqlite|microsoft\.data\.sqlite|rusqlite)\b", re.I),
    "SQLAlchemy": re.compile(r"\bsqlalchemy\b", re.I),
    "SQLx": re.compile(r"\bsqlx\b", re.I),           # Rust async SQL toolkit
    "Diesel (ORM)": re.compile(r"\bdiesel\b", re.I),  # Rust ORM/query builder
    "SQL Server": re.compile(
        r"\b(SqlConnection|(?:System|Microsoft)\.Data\.SqlClient)\b"),
    "Entity Framework": re.compile(r"\b(DbContext|EntityFrameworkCore|AddDbContext)\b"),
    "Redis": re.compile(r"\b(redis|StackExchange\.Redis)\b", re.I),
    "MongoDB": re.compile(r"\b(pymongo|mongodb|motor|MongoClient)\b", re.I),
    "Elasticsearch": re.compile(r"\belasticsearch\b", re.I),
    "S3": re.compile(r"\b(boto3|s3\.|aws_s3)\b", re.I),
}

# Message queues / brokers / messaging libraries, keyed by canonical name. Broad on
# purpose (recall over precision) and cross-language. The last entry catches generic
# pub/sub / message-bus structures that aren't a named broker.
_MESSAGING_RES = {
    "Kafka": re.compile(
        r"\b(kafka|confluent[_.]?kafka|aiokafka|rdkafka|KafkaProducer|KafkaConsumer"
        r"|sarama|segmentio/kafka)\b", re.I),
    "RabbitMQ/AMQP": re.compile(
        r"\b(rabbitmq|amqp|amqplib|pika|kombu|RabbitMQ\.Client|streadway|lapin)\b", re.I),
    "MQTT": re.compile(r"\b(mqtt|paho|mosquitto|aiomqtt|MqttClient)\b", re.I),
    "ZeroMQ": re.compile(r"\b(zeromq|pyzmq|libzmq|zmq|NetMQ)\b", re.I),
    "NATS": re.compile(r"\b(nats|async[_-]?nats|nats\.aio)\b", re.I),
    "Pulsar": re.compile(r"\b(pulsar|apache[_-]?pulsar)\b", re.I),
    "ActiveMQ/STOMP": re.compile(r"\b(activemq|stomp)\b", re.I),
    "NSQ": re.compile(r"\b(nsq|go-nsq)\b", re.I),
    # Bare `sqs`/`sns` are three-letter tokens that collide with unrelated protocol
    # text: an Ashtech GPS command string ("$PASHS,SNS,DUO,2") made secagent report an AWS
    # messaging dependency for an offline embedded UART parser with no networking at all.
    # Require real AWS context.
    "AWS SQS/SNS": re.compile(
        r"\b(?:aws[_.]?(?:sqs|sns)|(?:sqs|sns)[_.]?client|boto3?\.(?:client|resource)\("
        r"\s*[\"'](?:sqs|sns)[\"']|@aws-sdk/client-s(?:qs|ns))\b"
        r"|amazon(?:sqs|sns)",
        re.I),
    "Azure Service Bus": re.compile(
        r"\b(servicebus|ServiceBusClient|azure[._-]servicebus)\b", re.I),
    "GCP Pub/Sub": re.compile(r"\b(pubsub_v1|google\.cloud\.pubsub|gcloud[_-]?pubsub)\b", re.I),
    "MSMQ / .NET bus": re.compile(
        r"\b(MessageQueue|System\.Messaging|MSMQ|MassTransit|NServiceBus|Rebus)\b"),
    "ZeroMQ/nanomsg": re.compile(r"\b(nanomsg|nng)\b", re.I),
    # cFE Software Bus — the publish/subscribe backbone of NASA's cFS / cFE flight
    # software. Anchored to real API calls so it matches genuine usage, not type names
    # (e.g. the EDS ``..._PubSub_t`` types) or prose.
    "cFE Software Bus": re.compile(
        r"\bCFE_SB_(?:TransmitMsg|TransmitBuffer|SendMsg|Subscribe|SubscribeEx"
        r"|Unsubscribe|ReceiveBuffer|RcvMsg|CreatePipe|DeletePipe)\b"),
    # Generic pub/sub / message-bus structures that aren't a named broker. NOTE: a bare
    # ``pub[_]?sub`` term was removed — it matched the ``PubSub`` substring inside CamelCase
    # type names (e.g. cFS's ``..._PubSub_t``), inverting the architecture view. The
    # remaining terms are specific enough to be real bus libraries/identifiers.
    "Message bus (generic)": re.compile(
        r"\b(EventBus|MessageBus|MessageBroker|event_bus|message_bus|message_broker)\b",
        re.I),
}

# Raw network sockets (TCP / UDP / Unix-domain), cross-language. Anchored to socket APIs
# and the AF_*/SOCK_* constants so they match real usage, not prose. Transport is
# classified where the evidence is unambiguous; otherwise a generic socket is reported
# (suppressed once a specific transport is found — see ``find_sockets``).
_SOCKET_RES = {
    "TCP socket": re.compile(
        r"\bSOCK_STREAM\b|\bOS_SocketType_STREAM\b|\bTcpClient\b|\bTcpListener\b"
        # Rust std/tokio: TcpStream / TcpListener.
        r"|\bTcpStream\b|\bServerSocket\b|net\.(?:Listen|Dial)\(\s*[\"']tcp|\.createServer\("),
    "UDP socket": re.compile(
        r"\bSOCK_DGRAM\b|\bOS_SocketType_DATAGRAM\b|\bUdpClient\b|\bDatagramSocket\b"
        # Rust std/tokio: UdpSocket. recvfrom/sendto are common identifiers/wrappers —
        # require a call so a helper or field merely named `sendto` doesn't register.
        r"|\bUdpSocket\b|\.createSocket\(|net\.(?:ListenUDP|DialUDP)\b"
        r"|\brecvfrom\s*\(|\bsendto\s*\("),
    "Unix socket": re.compile(r"\bAF_UNIX\b|\bAF_LOCAL\b"),
    "Socket (generic)": re.compile(
        r"\bAF_INET6?\b|\bsocket\.socket\b|\bSystem\.Net\.Sockets\b"
        r"|\bOS_Socket(?:Open|Bind|Connect|Accept|RecvFrom|SendTo)\b"
        # getaddrinfo/setsockopt are weak anchors — require a call, not a bare mention.
        r"|\bgetaddrinfo\s*\(|\bsetsockopt\s*\("),
}


def find_endpoints(text: str) -> list[str]:
    out: list[str] = []
    for rx in _ENDPOINT_RES:
        out.extend(rx.findall(text))
    return _dedup(out)


def _is_noncall_host(host: str) -> bool:
    host = host.split(":", 1)[0].lower()
    # `schemas.*` / `ns.*` subdomains are namespace roots (schemas.microsoft.com,
    # schemas.xmlsoap.org, schemas.openxmlformats.org, …), never request targets.
    if host.startswith(("schemas.", "ns.")):
        return True
    return any(host == h or host.endswith("." + h) for h in _NONCALL_HOSTS)


def find_outbound(text: str) -> list[str]:
    """Hosts the file calls out to over HTTP.

    A quoted URL in source is, far more often than not, a license header, a doc link, an
    SCM/CI URL, or an XML/SOAP namespace — not a call. So a host counts only when the
    file shows evidence of actually driving an HTTP client (a real call/instantiation,
    not just the type name); namespace/schema lines and well-known license/standards
    hosts are dropped even then. Returns ``["(dynamic http client)"]`` when a client is
    used but the URL is built dynamically (no literal in the file).
    """
    if not _HTTP_CLIENT_RE.search(text):
        return []
    hosts: set[str] = set()
    for line in text.splitlines():
        if _NS_CONTEXT_RE.search(line):
            continue  # XML/SOAP namespace or serialization attribute — not a call
        hosts.update(h for h in _CALL_URL_RE.findall(line) if not _is_noncall_host(h))
    if not hosts:
        hosts.add("(dynamic http client)")
    return sorted(hosts)


def find_env_vars(text: str) -> list[str]:
    out: list[str] = []
    for rx in _ENV_RES:
        out.extend(rx.findall(text))
    # The .NET config indexer is only trustworthy when the file really uses IConfiguration.
    if _CONFIG_CONTEXT_RE.search(text):
        out.extend(_CONFIG_INDEXER_RE.findall(text))
    return _dedup(out)


# A quoted string that is nothing but a short name ending in a separator is a NAMING
# PREFIX, not a usage: `"cacheRedis": "redis-"` in an Azure resource-naming dictionary
# says how to name a cache if you ever make one, not that this component talks to Redis.
# Infrastructure repos ship these dictionaries as boilerplate, and eShop's produced two
# entirely fabricated architecture edges (`infra -> Redis`, `infra -> Kafka`) that flowed
# into the generated docs and the architecture diagram — where "accurate by construction"
# is the whole claim. Nothing else looks like this: a real reference is `redis:7`,
# `redis://host`, `StackExchange.Redis`, or a bare `redis`, none of which end in `-`/`_`.
_NAMING_PREFIX_RE = re.compile(r"\"[A-Za-z][A-Za-z0-9]{0,23}[-_]\"")


def _drop_naming_prefixes(text: str) -> str:
    return _NAMING_PREFIX_RE.sub('""', text)


def find_datastores(text: str) -> list[str]:
    text = _drop_naming_prefixes(text)
    return sorted(name for name, rx in _DATASTORE_RES.items() if rx.search(text))


def find_messaging(text: str) -> list[str]:
    """Message queues / brokers / messaging libraries the file uses (Kafka, MQTT, …)."""
    text = _drop_naming_prefixes(text)
    return sorted(name for name, rx in _MESSAGING_RES.items() if rx.search(text))


def find_sockets(text: str) -> list[str]:
    """Raw network socket usage (TCP / UDP / Unix-domain) the file performs.

    Transport-specific where the evidence is unambiguous (e.g. ``SOCK_STREAM`` → TCP);
    otherwise a single generic ``Socket`` is reported. The generic label is suppressed
    when a specific transport is found, so ``socket(AF_INET, SOCK_DGRAM)`` reports only
    "UDP socket", not both.
    """
    specific = sorted(name for name, rx in _SOCKET_RES.items()
                      if name != "Socket (generic)" and rx.search(text))
    if specific:
        return specific
    if _SOCKET_RES["Socket (generic)"].search(text):
        return ["Socket (generic)"]
    return []


def _dedup(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        if it not in seen:
            seen[it] = None
    return list(seen.keys())


def strip_comments(text: str) -> str:
    """Blank out ``//``, ``#`` and ``/* */`` comments so the detectors don't fire on
    commented-out or documented endpoints/URLs/brokers (a large false-positive class,
    since these regexes run on whole files).

    String-literal aware: a real ``"http://host"`` inside quotes is preserved intact, so
    stripping never destroys a genuine URL or breaks ``find_outbound``. Ambiguous cases
    (e.g. a lifetime tick in Rust) fall back to *keeping* text — never to hiding a signal.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""          # active string delimiter; "" when not inside a string
    in_block = False    # inside /* ... */
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        if quote:
            out.append(c)
            if c == "\\" and nxt:          # keep an escaped char verbatim
                out.append(nxt)
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in ("'", '"'):               # entering a string literal
            quote = c
            out.append(c)
        elif c == "/" and nxt == "/":     # // line comment
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "/" and nxt == "*":     # /* block comment
            in_block = True
            i += 2
            continue
        elif c == "#":                    # # line comment (Python/shell/YAML; harmless in C)
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)

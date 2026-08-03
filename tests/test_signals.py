"""Outbound-call detection: a URL is an http_call only with HTTP-client evidence."""

from __future__ import annotations

from secagent.affordances.signals import find_outbound

_APACHE_HEADER = (
    "/*\n * Licensed under the Apache License, Version 2.0;\n"
    " * see http://www.apache.org/licenses/LICENSE-2.0\n"
    " * Project home: https://cfs.gsfc.nasa.gov\n */\n"
)


def test_license_and_doc_urls_are_not_calls():
    # A C file with only license/doc URLs and no HTTP client → no outbound calls.
    src = _APACHE_HEADER + "int fm_app_main(void) { return 0; }\n"
    assert find_outbound(src) == []


def test_real_http_client_call_is_detected():
    src = _APACHE_HEADER + (
        "import httpx\n"
        'WORKER_URL = "http://worker:9000/enqueue"\n'
        "httpx.post(WORKER_URL, json={})\n"
    )
    hosts = find_outbound(src)
    assert any("worker" in h for h in hosts)
    # the Apache license host is dropped even though a client is present
    assert not any("apache.org" in h for h in hosts)


def test_scm_api_host_is_kept_when_called():
    # github.com/gitlab.com are legitimate API targets when actually called.
    src = 'import requests\nrequests.get("https://api.github.com/repos/x/y")\n'
    assert find_outbound(src) == ["api.github.com"]


def test_dynamic_client_without_literal_url():
    src = "import requests\nrequests.get(build_url(host))\n"
    assert find_outbound(src) == ["(dynamic http client)"]


def test_c_libcurl_call_is_detected():
    src = _APACHE_HEADER + (
        "curl_easy_setopt(h, CURLOPT_URL, \"https://api.sat.local/cmd\");\n"
        "curl_easy_perform(h);\n"
    )
    assert find_outbound(src) == ["api.sat.local"]


def test_dotnet_bare_httpclient_field_is_not_a_call():
    # A `HttpClient` field/param mention is not evidence of an outbound call, and the
    # quoted URL here is an XML namespace — neither should produce a host.
    src = (
        "using System.Net.Http;\n"
        "public class Svc {\n"
        "    private HttpClient _client;            // injected, not a call\n"
        '    [XmlRoot(Namespace="http://schemas.microsoft.com/2003/10/Serialization/")]\n'
        '    public string Ns = "http://tempuri.org/";\n'
        "}\n"
    )
    assert find_outbound(src) == []


def test_dotnet_real_httpclient_call_is_detected():
    src = (
        "using System.Net.Http;\n"
        'var resp = await _client.GetAsync("https://api.contoso.io/v1/orders");\n'
    )
    assert find_outbound(src) == ["api.contoso.io"]


def test_xml_namespace_url_is_skipped_even_with_client():
    # Real call + a stray namespace URL on its own line → only the call host survives.
    src = (
        'xmlns:soap = "http://schemas.xmlsoap.org/soap/envelope/"\n'
        'var r = await _client.PostAsync("https://api.contoso.io/cmd", body);\n'
    )
    assert find_outbound(src) == ["api.contoso.io"]

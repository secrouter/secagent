/**
 * Per-user SecSSO login for the "secrouter" pi provider (pi.dev).
 *
 * Registers a custom OAuth provider so `/login secrouter` drives SecSSO's OIDC
 * **device authorization** flow (RFC 8628) instead of a shared/static API key: each
 * developer authenticates as themselves, pi stores the resulting tokens in
 * `~/.pi/agent/auth.json` (0600) and refreshes them automatically, and every request
 * pi makes to SecRouter then carries THAT developer's own access token.
 *
 * This is the per-user counterpart to `secagent token` (see `src/secagent/secsso.py`
 * / `secagent/cli.py`): that helper is a SERVICE identity (OIDC client_credentials,
 * one shared token for automated/headless calls — e.g. `secagent index`/`scan`, or a
 * pi `models.json` provider using `apiKey: "!secagent token"`); this extension is a
 * PER-USER identity for an interactive `pi` session. Same SecSSO IdP, two different
 * OAuth grants for two different kinds of caller.
 *
 * ── The pi API this relies on, and how it was verified ──────────────────────────
 * pi was not runnable in the environment this file was written in (no global
 * install), so the shape below was checked directly against the actual published
 * npm packages rather than against pi's docs alone:
 *   - `pi.registerProvider(name, config)` and `ProviderConfig.oauth` —
 *     @earendil-works/pi-coding-agent@0.83.0, dist/core/extensions/types.d.ts
 *   - `OAuthLoginCallbacks` / `OAuthCredentials` —
 *     @earendil-works/pi-ai@0.83.0, dist/compat/extension-oauth-types.d.ts
 *     (that file's own comment: "Callback surface retained only for coding-agent
 *     extension compatibility" — i.e. this is the STABLE public extension contract,
 *     even though pi-ai has since grown a richer internal auth API alongside it).
 * pi/README.md and pi/models.example.json in this repo predate 0.83.0 ("verified
 * with pi 0.80.x") — re-verify this file's shape against whatever pi version is
 * actually deployed before relying on it, the same caveat pi/extensions/secagent.ts
 * already carries for its own (unrelated) API surface.
 *
 * KIMI GUARD: this file registers exactly one provider ("secrouter"). It does not
 * touch, enable, or reference pi's built-in "Kimi For Coding" provider (PRC
 * jurisdiction — excluded from this suite's model defaults).
 */

// Minimal structural typing so this compiles without pi's types present, matching
// pi/extensions/secagent.ts's convention. Trimmed to exactly what this file calls;
// see the .d.ts paths above for the authoritative, full shape.
interface OAuthCredentials {
  refresh: string;
  access: string;
  expires: number; // ms since epoch
  [key: string]: unknown;
}
interface OAuthLoginCallbacks {
  onAuth(info: { url: string; instructions?: string }): void;
  onDeviceCode(info: {
    userCode: string;
    verificationUri: string;
    intervalSeconds?: number;
    expiresInSeconds?: number;
  }): void;
  onPrompt(prompt: { message: string; placeholder?: string; allowEmpty?: boolean }): Promise<string>;
  onProgress?(message: string): void;
  onSelect(prompt: {
    message: string;
    options: { id: string; label: string }[];
  }): Promise<string | undefined>;
  signal?: AbortSignal;
}
interface ProviderConfig {
  name?: string;
  baseUrl?: string;
  api?: string;
  models?: unknown[];
  oauth?: {
    name: string;
    login(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials>;
    refreshToken(credentials: OAuthCredentials): Promise<OAuthCredentials>;
    getApiKey(credentials: OAuthCredentials): string;
  };
}
interface ExtensionAPI {
  registerProvider(name: string, config: ProviderConfig): void;
}

// ── SecSSO endpoints + this PUBLIC client's identity ────────────────────────────
//
// A device-code client is public (RFC 8628 SS3.1: "the device authorization request
// need not be authenticated") — no secret is embedded here, unlike secagent token's
// confidential client_credentials client. client_id defaults to "secagent-pi" so
// SecSSO can distinguish "a developer's interactive pi session" from "secagent"
// (the service identity secagent token authenticates as) in its own audit trail.
//
// Device/token URLs have no safe default (every SecSSO deployment's <domain> and
// realm differ) — set them via environment, matching how pi/extensions/secagent.ts
// already reads SECAGENT_REPO from process.env rather than hardcoding a path.
const DEVICE_URL = process.env.SECROUTER_SSO_DEVICE_URL || "";
const TOKEN_URL = process.env.SECROUTER_SSO_TOKEN_URL || "";
const CLIENT_ID = process.env.SECROUTER_SSO_CLIENT_ID || "secagent-pi";
const SCOPE = process.env.SECROUTER_SSO_SCOPE || "openid profile email secrouter";
// Placeholder — every real deployment overrides this via models.json's own
// provider-level `baseUrl` (see pi/models.example.json in this repo), the same way
// every other example provider in this repo treats baseUrl as operator-supplied.
const DEFAULT_BASE_URL = process.env.SECROUTER_BASE_URL || "https://secrouter.example.com:47002/v1";

interface DeviceAuthorizationResponse {
  device_code: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete?: string;
  expires_in: number;
  interval?: number;
}

interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  expires_in: number;
  token_type?: string;
  error?: string;
  error_description?: string;
}

function requireConfigured(): void {
  if (!DEVICE_URL || !TOKEN_URL) {
    throw new Error(
      "secrouter-auth: SECROUTER_SSO_DEVICE_URL and SECROUTER_SSO_TOKEN_URL must be " +
        "set (SecSSO's OIDC device_authorization_endpoint / token_endpoint) before " +
        "/login secrouter can run.",
    );
  }
}

async function postForm(url: string, body: Record<string, string>): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(body).toString(),
  });
}

/** RFC 8628 SS3.4: poll the token endpoint until the user completes the device flow. */
async function pollDeviceToken(
  deviceCode: string,
  intervalSeconds: number,
  expiresInSeconds: number,
  callbacks: OAuthLoginCallbacks,
): Promise<TokenResponse> {
  const deadline = Date.now() + expiresInSeconds * 1000;
  let interval = Math.max(1, intervalSeconds);
  while (Date.now() < deadline) {
    if (callbacks.signal?.aborted) throw new Error("Login cancelled");
    await new Promise((resolve) => setTimeout(resolve, interval * 1000));
    const resp = await postForm(TOKEN_URL, {
      grant_type: "urn:ietf:params:oauth:grant-type:device_code",
      device_code: deviceCode,
      client_id: CLIENT_ID,
    });
    const data = (await resp.json()) as TokenResponse;
    if (resp.ok && data.access_token) return data;
    if (data.error === "authorization_pending") continue;
    if (data.error === "slow_down") {
      interval += 5; // RFC 8628 SS3.5: back off by 5s and keep polling.
      continue;
    }
    // access_denied, expired_token, or anything else SecSSO returns: not recoverable.
    throw new Error(
      `SecSSO device login failed: ${data.error || resp.status} ${data.error_description || ""}`.trim(),
    );
  }
  throw new Error("SecSSO device code expired before login completed");
}

async function login(callbacks: OAuthLoginCallbacks): Promise<OAuthCredentials> {
  requireConfigured();
  const authResp = await postForm(DEVICE_URL, { client_id: CLIENT_ID, scope: SCOPE });
  if (!authResp.ok) {
    throw new Error(`SecSSO device_authorization request failed: ${authResp.status}`);
  }
  const auth = (await authResp.json()) as DeviceAuthorizationResponse;
  callbacks.onDeviceCode({
    userCode: auth.user_code,
    verificationUri: auth.verification_uri_complete || auth.verification_uri,
    intervalSeconds: auth.interval ?? 5,
    expiresInSeconds: auth.expires_in,
  });
  const tokens = await pollDeviceToken(
    auth.device_code,
    auth.interval ?? 5,
    auth.expires_in,
    callbacks,
  );
  if (!tokens.refresh_token) {
    // Not fatal to THIS login, but refreshToken() below has nothing to exchange —
    // surface it now rather than fail silently on the first refresh, days later.
    callbacks.onProgress?.(
      "Warning: SecSSO did not return a refresh_token; re-run /login secrouter once " +
        "this access token expires.",
    );
  }
  return {
    refresh: tokens.refresh_token ?? "",
    access: tokens.access_token,
    expires: Date.now() + tokens.expires_in * 1000,
  };
}

async function refreshToken(credentials: OAuthCredentials): Promise<OAuthCredentials> {
  requireConfigured();
  if (!credentials.refresh) {
    throw new Error("no refresh_token on file — run /login secrouter again");
  }
  const resp = await postForm(TOKEN_URL, {
    grant_type: "refresh_token",
    refresh_token: credentials.refresh,
    client_id: CLIENT_ID,
  });
  const data = (await resp.json()) as TokenResponse;
  if (!resp.ok || !data.access_token) {
    throw new Error(
      `SecSSO token refresh failed: ${data.error || resp.status} ${data.error_description || ""}`.trim(),
    );
  }
  return {
    // Some IdPs rotate the refresh token on every use, some don't — keep the old one
    // if SecSSO doesn't issue a new one, rather than dropping it and forcing a
    // re-login on the NEXT refresh.
    refresh: data.refresh_token ?? credentials.refresh,
    access: data.access_token,
    expires: Date.now() + data.expires_in * 1000,
  };
}

function getApiKey(credentials: OAuthCredentials): string {
  return credentials.access;
}

export default function (pi: ExtensionAPI): void {
  pi.registerProvider("secrouter", {
    name: "SecRouter",
    baseUrl: DEFAULT_BASE_URL,
    api: "openai-completions", // SecRouter speaks the OpenAI-compatible /v1/chat/completions shape
    // No `models` here on purpose: the deployment-specific model catalog belongs in
    // the operator's own models.json (see pi/models.example.json), which merges its
    // `models`/`baseUrl` onto this registration by provider id — the same pattern
    // models.md documents for Ollama/vLLM/LM Studio. authHeader is NOT needed:
    // `api: "openai-completions"` already sends the resolved key (here, the OAuth
    // access token via getApiKey) as `Authorization: Bearer <key>` on its own.
    oauth: { name: "SecRouter (SecSSO)", login, refreshToken, getApiKey },
  });
}

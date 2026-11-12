// node --experimental-default-type=module --test src/security-headers.test.js
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  APEX_HOST,
  CONTENT_SECURITY_POLICY,
  HSTS_VALUE,
  LEGACY_REDIRECTS,
  PERMISSIONS_POLICY,
  applySecurityHeaders,
  handleRequest,
  redirectTarget,
  shouldSendHsts,
} from "./security.js";

function request(url, headers) {
  return new Request(url, { headers });
}

test("legacy redirects in the Worker match public/_redirects", () => {
  const file = readFileSync(new URL("../public/_redirects", import.meta.url), "utf8");
  const fromFile = new Map();
  for (const line of file.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const [source, destination, code] = trimmed.split(/\s+/);
    assert.equal(code, "301");
    assert.equal(fromFile.has(source), false);
    fromFile.set(source, destination);
  }
  assert.equal(fromFile.size, LEGACY_REDIRECTS.size);
  for (const [source, destination] of fromFile) {
    assert.equal(LEGACY_REDIRECTS.get(source), destination);
  }
});

test("https apex is not redirected", () => {
  const url = "https://migratestate.com/contact?ref=1";
  assert.equal(redirectTarget(request(url)), null);
});

test("http and www each 301 once to the https apex, preserving path and query", () => {
  const cases = [
    ["http://migratestate.com/contact", "https://migratestate.com/contact"],
    ["http://migratestate.com/contact?ref=scan", "https://migratestate.com/contact?ref=scan"],
    ["https://www.migratestate.com/buying-property-spain", "https://migratestate.com/buying-property-spain"],
    ["http://www.migratestate.com/contact?ref=scan", "https://migratestate.com/contact?ref=scan"],
    ["https://WWW.MigrateState.com/mexico", "https://migratestate.com/mexico"],
    ["http://migratestate.com:80/privacy", "https://migratestate.com/privacy"],
    ["http://www.migratestate.com/", "https://migratestate.com/"],
    ["https://www.migratestate.com/", "https://migratestate.com/"],
  ];
  for (const [from, to] of cases) {
    assert.equal(redirectTarget(request(from)), to, from);
  }
});

test("legacy paths canonicalize host and scheme in the same hop", () => {
  assert.equal(
    redirectTarget(request("https://migratestate.com/owning-property-abroad-tax-reporting")),
    "https://migratestate.com/us-tax-foreign-property",
  );
  assert.equal(
    redirectTarget(request("http://www.migratestate.com/owning-property-abroad-tax-reporting.html?utm=old")),
    "https://migratestate.com/us-tax-foreign-property?utm=old",
  );
});

test("preview and local hosts are not sent to the apex", () => {
  assert.equal(redirectTarget(request("http://localhost:8787/contact")), null);
  assert.equal(redirectTarget(request("https://migratestate.example.workers.dev/contact")), null);
  assert.equal(
    redirectTarget(request("http://127.0.0.1:8787/owning-property-abroad-tax-reporting")),
    "http://127.0.0.1:8787/us-tax-foreign-property",
  );
});

test("HSTS is https-only, production-only, and has no preload", () => {
  assert.equal(HSTS_VALUE, "max-age=31536000; includeSubDomains");
  assert.equal(HSTS_VALUE.includes("preload"), false);
  assert.equal(shouldSendHsts(request("https://migratestate.com/")), true);
  assert.equal(shouldSendHsts(request("https://www.migratestate.com/")), true);
  assert.equal(shouldSendHsts(request("http://migratestate.com/")), false);
  assert.equal(shouldSendHsts(request("http://www.migratestate.com/")), false);
  assert.equal(shouldSendHsts(request("https://localhost/")), false);
  assert.equal(shouldSendHsts(request("http://localhost/")), false);
});

test("security headers match the requested policy and CSP stays report-only", () => {
  const headers = new Headers();
  applySecurityHeaders(headers, request("https://migratestate.com/"));
  assert.equal(headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(headers.get("X-Frame-Options"), "SAMEORIGIN");
  assert.equal(headers.get("Referrer-Policy"), "strict-origin-when-cross-origin");
  assert.equal(headers.get("Permissions-Policy"), PERMISSIONS_POLICY);
  assert.equal(headers.get("Strict-Transport-Security"), HSTS_VALUE);
  assert.equal(headers.get("Content-Security-Policy"), null);
  assert.equal(headers.get("Content-Security-Policy-Report-Only"), CONTENT_SECURITY_POLICY);

  for (const token of ["camera=()", "microphone=()", "geolocation=()", "payment=()", "usb=()"]) {
    assert.equal(PERMISSIONS_POLICY.includes(token), true, token);
  }
  for (const token of [
    "default-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'self'",
    "script-src",
    "'unsafe-inline'",
    "https://*.googletagmanager.com",
    "https://*.googlesyndication.com",
    "https://*.hs-scripts.com",
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://flagcdn.com",
    "https://api-eu1.hsforms.com",
    "https://assets.mailerlite.com",
  ]) {
    assert.equal(CONTENT_SECURITY_POLICY.includes(token), true, token);
  }
  assert.equal(CONTENT_SECURITY_POLICY.includes("report-uri"), false);
  assert.ok(CONTENT_SECURITY_POLICY.length < 6000);
});

test("fetch redirects without calling assets and keeps the asset body otherwise", async () => {
  let fetches = 0;
  const env = {
    ASSETS: {
      async fetch() {
        fetches += 1;
        return new Response("<!doctype html><title>ok</title>", {
          status: 200,
          headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=60" },
        });
      },
    },
  };

  const moved = await handleRequest(request("http://www.migratestate.com/contact?x=1"), env);
  assert.equal(fetches, 0);
  assert.equal(moved.status, 301);
  assert.equal(moved.headers.get("Location"), "https://migratestate.com/contact?x=1");
  assert.equal(moved.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(moved.headers.get("Strict-Transport-Security"), null);
  assert.equal(await moved.text(), "");

  const page = await handleRequest(request("https://migratestate.com/"), env);
  assert.equal(fetches, 1);
  assert.equal(page.status, 200);
  assert.equal(page.headers.get("content-type"), "text/html; charset=utf-8");
  assert.equal(page.headers.get("cache-control"), "public, max-age=60");
  assert.equal(page.headers.get("Strict-Transport-Security"), HSTS_VALUE);
  assert.equal(page.headers.get("Content-Security-Policy"), null);
  assert.equal(await page.text(), "<!doctype html><title>ok</title>");
  assert.equal(APEX_HOST, "migratestate.com");
});

test("304 responses stay bodyless", async () => {
  const env = {
    ASSETS: {
      async fetch() {
        return new Response(null, { status: 304, headers: { etag: '"abc"' } });
      },
    },
  };
  const response = await handleRequest(request("https://migratestate.com/style.css"), env);
  assert.equal(response.status, 304);
  assert.equal(response.headers.get("etag"), '"abc"');
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(await response.text(), "");
});

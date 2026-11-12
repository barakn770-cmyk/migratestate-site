/**
 * Edge middleware for the migratestate.com static-asset Worker.
 *
 * Canonical host is https://migratestate.com — that is the host in every
 * rel=canonical tag, og:url, robots.txt, and sitemap.xml. Requests to
 * www.migratestate.com, and requests whose URL is still http:, 301 to that
 * host in one hop. Other hostnames (workers.dev, localhost) are left alone
 * so previews and `wrangler dev` keep working.
 *
 * Security headers are set here, not in public/_headers. src/index.js runs
 * before asset serving (assets.run_worker_first), and Cloudflare does not
 * apply _headers / _redirects to responses the Worker returns. The two
 * legacy paths in public/_redirects are repeated below so they still 301.
 */

export const APEX_HOST = "migratestate.com";

/** Same rules as public/_redirects. Values are pathnames on the canonical host. */
export const LEGACY_REDIRECTS = new Map([
  ["/owning-property-abroad-tax-reporting", "/us-tax-foreign-property"],
  ["/owning-property-abroad-tax-reporting.html", "/us-tax-foreign-property"],
]);

export const HSTS_VALUE = "max-age=31536000; includeSubDomains";

// Hardware and payment capabilities this site never uses. autoplay,
// encrypted-media, fullscreen, and sync-xhr stay at the browser default
// so AdSense and the HubSpot embed are not blocked when this is enforced.
export const PERMISSIONS_POLICY = [
  "accelerometer=()",
  "bluetooth=()",
  "camera=()",
  "display-capture=()",
  "geolocation=()",
  "gyroscope=()",
  "hid=()",
  "interest-cohort=()",
  "magnetometer=()",
  "microphone=()",
  "midi=()",
  "payment=()",
  "serial=()",
  "usb=()",
].join(", ");

// Report-only on purpose. An enforcing policy would need 'unsafe-inline'
// (gtag bootstrap, the contact and get-started submitters, the MailerLite
// onsubmit handler, style attributes) and 'unsafe-eval' (AdSense and the
// HubSpot loader). Those two embeds also pull scripts, frames, and pixels
// from hosts that change. The allowlist below is the inventory of what the
// pages actually reference, plus the host wildcards Google and HubSpot
// publish for these products, so the console is reserved for surprises.
//
// First-party: same-origin HTML, style.css, images.
// Scripts: www.googletagmanager.com/gtag/js (every page, plus an inline
//   config), pagead2.googlesyndication.com/pagead/js/adsbygoogle.js (guide
//   pages), //js-eu1.hs-scripts.com/148691139.js (HubSpot, every page).
// Styles/fonts: fonts.googleapis.com CSS, fonts.gstatic.com files.
// Images: flagcdn.com flags on the homepage. No iframes in the HTML.
// Forms: contact and get-started POST via fetch() to
//   api-eu1.hsforms.com. The homepage checklist form POSTs to
//   assets.mailerlite.com (no MailerLite script tag). No <form action>.
const GOOGLE_SCRIPT = [
  "https://*.googletagmanager.com",
  "https://*.google-analytics.com",
  "https://*.analytics.google.com",
  "https://*.googlesyndication.com",
  "https://*.googleadservices.com",
  "https://*.googletagservices.com",
  "https://*.google.com",
  "https://*.gstatic.com",
  "https://*.doubleclick.net",
];

const HUBSPOT_SCRIPT = [
  "https://*.hs-scripts.com",
  "https://*.hs-analytics.net",
  "https://*.hs-banner.com",
  "https://*.hscollectedforms.net",
  "https://*.hsadspixel.net",
  "https://*.hsforms.net",
  "https://*.hsforms.com",
  "https://*.hubspot.com",
  "https://*.hsappstatic.net",
  "https://*.usemessages.com",
  "https://*.hubspotfeedback.com",
  "https://*.hsleadflows.net",
];

export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  "form-action 'self' https://api-eu1.hsforms.com https://assets.mailerlite.com",
  `script-src 'self' 'unsafe-inline' 'unsafe-eval' ${[...GOOGLE_SCRIPT, ...HUBSPOT_SCRIPT].join(" ")}`,
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://*.gstatic.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  `img-src 'self' data: blob: https://flagcdn.com ${[
    ...GOOGLE_SCRIPT,
    "https://*.hubspot.com",
    "https://*.hsforms.com",
    "https://*.hsforms.net",
    "https://*.hubspot.net",
    "https://*.hubspotusercontent.net",
    "https://*.hubspotusercontent-eu1.net",
    "https://*.hubspotusercontent-na1.net",
  ].join(" ")}`,
  `connect-src 'self' https://api-eu1.hsforms.com https://assets.mailerlite.com https://fonts.googleapis.com https://fonts.gstatic.com ${[
    ...GOOGLE_SCRIPT,
    "https://*.hubspot.com",
    "https://*.hubapi.com",
    "https://*.hsforms.com",
    "https://*.hscollectedforms.net",
    "https://*.hs-banner.com",
    "https://*.hs-analytics.net",
  ].join(" ")}`,
  `frame-src 'self' ${[
    "https://*.googlesyndication.com",
    "https://*.googleadservices.com",
    "https://*.googletagmanager.com",
    "https://*.google.com",
    "https://*.doubleclick.net",
    "https://*.hubspot.com",
    "https://*.hsforms.com",
    "https://*.hsforms.net",
  ].join(" ")}`,
  "worker-src 'self' blob:",
  "media-src 'self' blob: https://*.googlesyndication.com https://*.doubleclick.net https://*.gstatic.com",
  "manifest-src 'self'",
].join("; ");

const PRODUCTION_HOSTS = new Set([APEX_HOST, `www.${APEX_HOST}`]);

export function redirectTarget(request) {
  const url = new URL(request.url);
  const host = url.hostname.toLowerCase();
  const production = PRODUCTION_HOSTS.has(host);
  const legacy = LEGACY_REDIRECTS.get(url.pathname);

  if (!legacy && !production) return null;

  const dest = new URL(url.href);
  if (legacy) dest.pathname = legacy;

  if (production) {
    const needsHttps = url.protocol !== "https:";
    const needsApex = host !== APEX_HOST;
    if (!legacy && !needsHttps && !needsApex) return null;
    dest.protocol = "https:";
    dest.hostname = APEX_HOST;
    dest.port = "";
  }

  return dest.href;
}

export function shouldSendHsts(request) {
  const url = new URL(request.url);
  if (!PRODUCTION_HOSTS.has(url.hostname.toLowerCase())) return false;
  // Browsers ignore HSTS on responses delivered over HTTP. Attach it only
  // when this response itself is HTTPS (the apex page, or the www → apex hop).
  return url.protocol === "https:";
}

export function applySecurityHeaders(headers, request) {
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "SAMEORIGIN");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", PERMISSIONS_POLICY);
  headers.set("Content-Security-Policy-Report-Only", CONTENT_SECURITY_POLICY);
  if (shouldSendHsts(request)) {
    headers.set("Strict-Transport-Security", HSTS_VALUE);
  } else {
    headers.delete("Strict-Transport-Security");
  }
}

function withSecurityHeaders(response, request) {
  const headers = new Headers(response.headers);
  applySecurityHeaders(headers, request);
  // 204/205/304 responses are required to have a null body.
  const nullBody = response.status === 204 || response.status === 205 || response.status === 304;
  return new Response(nullBody ? null : response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export async function handleRequest(request, env) {
  const target = redirectTarget(request);
  if (target) {
    const headers = new Headers();
    applySecurityHeaders(headers, request);
    headers.set("Location", target);
    return new Response(null, { status: 301, headers });
  }

  if (!env || !env.ASSETS || typeof env.ASSETS.fetch !== "function") {
    const headers = new Headers();
    applySecurityHeaders(headers, request);
    return new Response("Static asset binding is not configured.", {
      status: 500,
      headers,
    });
  }

  const asset = await env.ASSETS.fetch(request);
  return withSecurityHeaders(asset, request);
}

// Requests that need an identity: changes, and operator-only reads such as charter logs.
// Behind an authenticating proxy the browser is already signed in, so a request is first sent
// without a token; only when that is refused is a token asked for. A token that works is kept in
// this browser until it is refused or forgotten, so each person pastes theirs once per browser.
const LT_TOKEN = "labtender-token";

function ltToken() {
  try { return localStorage.getItem(LT_TOKEN); } catch (e) { return null; /* storage blocked */ }
}

function ltKeep(token) {
  try { localStorage.setItem(LT_TOKEN, token); } catch (e) { /* kept for this request only */ }
  ltShowForget();
}

function ltForget() {
  try { localStorage.removeItem(LT_TOKEN); } catch (e) { /* storage blocked: nothing kept */ }
  ltShowForget();
}

// fetch() with labtender's identity. role names the role in the prompt ("operator", "purser").
// With ask false it never prompts: a refresh in the background must not interrupt anyone.
async function ltFetch(url, opts = {}, role = "operator", ask = true) {
  const send = (token) => fetch(url, {
    cache: "no-store",
    ...opts,
    headers: {
      "X-Requested-With": "labtender",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers || {}),
    },
  });
  const kept = ltToken();
  let r = await send(kept);
  // 401: no identity, or a kept token that was revoked (forget it). 403: an identity without the
  // role this needs; a kept token stays, since it still serves for what it is allowed.
  if (r.status === 401 && kept) ltForget();
  if (r.status !== 401 && r.status !== 403) return r;
  if (!ask) return r;
  const why = r.status === 403 && kept ? " (the kept token lacks this role)" : "";
  const article = /^[aeiou]/.test(role) ? "An" : "A";
  const token = prompt(`${article} ${role} token${why}. It is kept in this browser until you forget it:`);
  if (!token) return r;
  r = await send(token);
  if (r.status !== 401 && r.status !== 403) ltKeep(token);  // accepted, whatever the change's fate
  return r;
}

// A "forget token" control in the page header, shown while this browser keeps a token.
function ltShowForget() {
  const header = document.querySelector("header nav") || document.querySelector("header");
  if (!header) return;
  let b = document.getElementById("lt-forget");
  if (!b) {
    b = document.createElement("button");
    b.id = "lt-forget";
    b.className = "meta";
    b.textContent = "forget token";
    b.title = "Forget the token this browser keeps for changes";
    Object.assign(b.style, { background: "none", border: "none", padding: "0",
                             fontFamily: "inherit", cursor: "pointer", textDecoration: "underline" });
    b.addEventListener("click", ltForget);
    header.append(b);
  }
  b.hidden = !ltToken();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ltShowForget);
else ltShowForget();

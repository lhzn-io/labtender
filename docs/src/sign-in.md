# Signing in through a proxy

Out of the box, the board asks for a token the first time a change needs one, and keeps a token
that works in that browser until it is refused or forgotten (the *forget token* link in the
page header). A lab with an identity provider (Microsoft Entra ID, Google, Okta, Keycloak,
Authentik and others) can instead put an authenticating proxy in front of labtender: people sign
in with their usual account, and the board never asks for a token. Agents and the CLI keep their
bearer tokens.

labtender does not talk to the identity provider itself. A proxy such as
[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) does that, and passes the signed-in
user to labtender in a header. labtender's part is small:

- It believes the header only from the proxy's own addresses (`trusted`), and only when the
  request also carries a secret shared with the proxy (`X-Labtender-Proxy-Secret`). Otherwise the
  header is ignored. The address alone is not enough: any other process on the proxy's machine
  connects from the same address, and so can a web page that points its own name at
  `127.0.0.1`. Neither knows the secret.
- It maps the user to a labtender identity and roles through an allow-list. Anyone signed in but
  not on the list is refused. Roles come only from the list, never from the identity provider.
- A bearer token, when one is sent, always decides. A bad token is refused, never rescued by the
  header.
- The proxy signs in people only: an identity that has ever held an agent token cannot be on the
  list, and `purser` is granted only by naming it.
- A change made with the proxy's identity must carry `X-Requested-With: labtender`, which the
  board sends. A page on another site can make a browser send a form with the proxy's cookie, but
  cannot add that header.

## Configure labtender

Write `$LABTENDER_HOME/proxy.toml` and restart the service:

```toml
header = "X-Auth-Request-Email"      # the header the proxy sets
trusted = ["127.0.0.1", "::1"]       # the proxy's addresses, as labtender sees them
secret = "<at least 32 characters>"  # the proxy adds it to every request as X-Labtender-Proxy-Secret

[[allow]]
user = "dana@example.org"            # the header's value (compared ignoring case)
identity = "dana"                    # the labtender identity: claims and approvals carry it

[[allow]]
user = "pat@example.org"
identity = "pat"
roles = ["viewer", "operator", "admin", "purser"]   # default: viewer, operator, admin
```

Make the secret with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, and keep
`proxy.toml` readable only by the service's user. Without `proxy.toml` nothing changes. A file
labtender cannot use (an unknown role, an agent identity, `Authorization` as the header, a
missing or short secret) stops the service at start, so a mistake is never silently ignored. `labtender serve` uses the real peer address of each connection and never one
from `X-Forwarded-For`, so `trusted` means what it says. `GET /api/whoami` answers with
`"via": "proxy"` for an identity the proxy supplied.

## Example: Caddy and oauth2-proxy with Microsoft Entra ID

Caddy serves HTTPS and asks oauth2-proxy whether each browser request is signed in
(`forward_auth`). Requests that carry a bearer token go straight to labtender, which checks the
token itself.

1. **Register an app** in Entra ID: single tenant, a *Web* redirect URI of
   `https://lab.example.org/oauth2/callback`, and a client secret. Save the secret in a file
   readable only by the proxy's user.
2. **Run oauth2-proxy** on the same machine:

   ```bash
   oauth2-proxy \
     --provider=entra-id \
     --oidc-issuer-url=https://login.microsoftonline.com/<tenant-id>/v2.0 \
     --client-id=<application-id> --client-secret-file=/path/to/client-secret \
     --cookie-secret-file=/path/to/cookie-secret --cookie-secure=true \
     --email-domain=example.org \
     --redirect-url=https://lab.example.org/oauth2/callback \
     --http-address=127.0.0.1:4180 --reverse-proxy=true \
     --set-xauthrequest=true --upstream=static://202
   ```

   If your tenant's accounts have no `email` claim, oauth2-proxy can use the sign-in name
   instead (`--oidc-email-claim=preferred_username`); the allow-list then names those.
3. **Configure Caddy**:

   ```text
   lab.example.org {
       # Agents and the CLI: a bearer token goes straight to labtender.
       @bearer header Authorization "Bearer *"
       handle @bearer {
           request_header -X-Auth-Request-Email
           reverse_proxy 127.0.0.1:8795
       }
       handle /oauth2/* {
           reverse_proxy 127.0.0.1:4180 {
               header_up X-Real-IP {remote_host}
               header_up X-Forwarded-Uri {uri}
           }
       }
       # Everyone else signs in first.
       handle {
           request_header -X-Auth-Request-Email
           forward_auth 127.0.0.1:4180 {
               uri /oauth2/auth
               header_up X-Real-IP {remote_host}
               copy_headers X-Auth-Request-Email
               @signin status 401
               handle_response @signin {
                   redir * /oauth2/sign_in?rd={scheme}://{host}{uri}
               }
           }
           # The secret from proxy.toml, here read from the environment Caddy runs in.
           request_header X-Labtender-Proxy-Secret {$LABTENDER_PROXY_SECRET}
           reverse_proxy 127.0.0.1:8795
       }
   }
   ```

   Removing `X-Auth-Request-Email` from incoming requests means only oauth2-proxy can set it.
4. **Point agents and the CLI at the new address**: `LABTENDER_URL=https://lab.example.org`.
   Their tokens work unchanged.

labtender can keep listening on its own port for the lab network as well: requests that reach it
directly are not from a trusted address, so they need a token as before. To allow only the
proxy, leave `labtender serve --bind` at its default, `127.0.0.1`.

## Example: Cloudflare Access and a Cloudflare Tunnel

With a domain on Cloudflare, Cloudflare Access can do the sign-in (with Google, Microsoft Entra
ID, GitHub and other identity providers, several at once) and a Cloudflare Tunnel can carry the
traffic, so the labtender service's machine needs only `cloudflared`: no open port, no
certificates and no other proxy.

1. **Create an Access application** for the board's hostname, for example `lab.example.org`, in
   the Cloudflare Zero Trust dashboard, with a policy that allows the people (or the email
   domain) who should sign in. Do this before the next steps, so the hostname is never reachable
   without sign-in.
2. **Route the hostname through a tunnel** to the labtender service on the same machine. In the
   tunnel's `config.yml`:

   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /path/to/<tunnel-id>.json
   ingress:
     - hostname: lab.example.org
       service: http://localhost:8795
     - service: http_status:404
   ```

   and create its DNS record with `cloudflared tunnel route dns <tunnel-id> lab.example.org`.
   Run `cloudflared tunnel run` as a service.
3. **Configure labtender** with the header Access sets:

   ```toml
   header = "Cf-Access-Authenticated-User-Email"
   trusted = ["127.0.0.1", "::1"]   # cloudflared connects from this machine
   secret = "<at least 32 characters>"

   [[allow]]
   user = "dana@example.org"
   identity = "dana"
   ```

4. **Add the secret at Cloudflare's edge** with a request header Transform Rule for the
   hostname: *Set static*, header `X-Labtender-Proxy-Secret`, value the secret. The edge
   overwrites whatever a browser sent under that name.

Agents and the CLI can keep using the service's address on the lab network with their tokens;
requests through the tunnel without an Access sign-in never reach labtender. `cloudflared`
connects to labtender from the same machine, but another local process that sends the header
without the secret is ignored.

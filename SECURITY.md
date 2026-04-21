# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.2.x   | ✅ Yes     |
| < 1.0.0 | ❌ No      |

Only the latest release receives security fixes. Please update before reporting.

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

If you discover a security issue, please report it responsibly:

1. Open a [GitHub Security Advisory](https://github.com/your-username/alldebrid-client/security/advisories/new) (preferred), or
2. Send an email to the maintainer (see repository profile).

Please include:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fix (optional but appreciated)

You can expect an acknowledgement within **48 hours** and a fix or mitigation plan within **7 days** for confirmed issues.

---

## Security Considerations

### API Key Storage

Your AllDebrid API key is stored in `config/config.json` on disk. Ensure this file is not world-readable:

```bash
chmod 600 config/config.json
```

When running in Docker, do not expose the config volume publicly.

### Web UI

The Web UI has **no authentication** by default. It is intended to run on a trusted local network or behind a reverse proxy with authentication (e.g. Nginx + Basic Auth, Authelia, Authentik).

**Do not expose port 8080 directly to the internet.**

Example Nginx snippet with Basic Auth:

```nginx
location / {
    auth_basic "AllDebrid-Client";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://localhost:8080;
}
```

### Discord Webhook URL

Treat your Discord webhook URL as a secret — anyone with the URL can post to your channel. Store it only in `config/config.json` and do not commit it to version control.

### .gitignore

The provided `.gitignore` excludes `config/` and `data/` from version control. Do not remove these entries.

---

## Scope

The following are **in scope** for security reports:

- API key or webhook URL exposure
- Remote code execution
- Path traversal in file download
- Authentication bypass (if auth is added in future)

The following are **out of scope**:

- Issues in AllDebrid's own API
- Vulnerabilities in third-party dependencies (report upstream)
- Denial of service on a local instance with no network exposure

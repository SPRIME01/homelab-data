# Secure External Access to Home Assistant

This guide outlines the steps to configure secure external access to your Home Assistant instance running in a homelab environment using Cloudflare Tunnel.

## Prerequisites

*   A running Home Assistant instance.
*   A Cloudflare account.
*   A registered domain name managed by Cloudflare.
*   `cloudflared` daemon installed on a machine within your homelab (can be the Home Assistant host or another server/VM).

## 1. Configure Cloudflare Tunnel Access

Cloudflare Tunnel creates a secure, outbound-only connection between your Home Assistant instance and Cloudflare's edge, eliminating the need for open firewall ports.

1.  **Install `cloudflared`:** Follow the official Cloudflare documentation to install `cloudflared` on your chosen machine.
2.  **Login to Cloudflare:**
    ```bash
    cloudflared login
    ```
    Follow the prompts to authorize `cloudflared` with your Cloudflare account.
3.  **Create a Tunnel:**
    ```bash
    cloudflared tunnel create home-assistant-tunnel
    ```
    Note the tunnel UUID and the path to the credentials file (`.cloudflared/<tunnel-uuid>.json`).
4.  **Configure DNS:** Create a CNAME record in your Cloudflare DNS settings pointing your desired subdomain (e.g., `ha.yourdomain.com`) to your tunnel URL (`<tunnel-uuid>.cfargotunnel.com`).
    ```bash
    cloudflared tunnel route dns home-assistant-tunnel ha.yourdomain.com
    ```
5.  **Configure the Tunnel:** Create or edit the `cloudflared` configuration file (usually `~/.cloudflared/config.yml` or `/etc/cloudflared/config.yml`).
    ```yaml
    # filepath: ~/.cloudflared/config.yml or /etc/cloudflared/config.yml
    tunnel: <tunnel-uuid>
    credentials-file: /path/to/.cloudflared/<tunnel-uuid>.json # Adjust path as needed

    ingress:
      - hostname: ha.yourdomain.com
        service: http://<home-assistant-ip>:8123 # Replace with your HA IP/hostname and port
      # Catch-all rule MUST be the last rule
      - service: http_status:404
    ```
6.  **Run the Tunnel:**
    *   **Foreground (for testing):** `cloudflared tunnel run home-assistant-tunnel`
    *   **As a service (recommended):** `sudo cloudflared service install` followed by `sudo systemctl start cloudflared` (or equivalent for your OS).

## 2. Set Up Authentication Requirements (Cloudflare Access)

Add an extra layer of security before users even reach Home Assistant's login page.

1.  **Navigate to Cloudflare Zero Trust Dashboard:** Go to `Access` -> `Applications`.
2.  **Add an Application:** Choose `Self-hosted`.
3.  **Configure Application:**
    *   **Application Name:** Home Assistant Access (or similar)
    *   **Session Duration:** Choose an appropriate duration (e.g., 24 hours).
    *   **Application Domain:** Enter the subdomain you configured (`ha.yourdomain.com`).
    *   **Identity providers:** Select your preferred providers (e.g., Email OTP, GitHub, Google).
    *   **Instant Auth:** Optionally enable if desired.
4.  **Add Policies:**
    *   Create a policy (e.g., "Allow Admins").
    *   **Action:** Allow
    *   **Configure rules:** Define who can access (e.g., specific email addresses, members of a GitHub organization).
5.  **Save Configuration.**

## 3. Establish TLS Encryption

Cloudflare handles TLS encryption between the client and Cloudflare's edge. The tunnel ensures encryption between Cloudflare and your `cloudflared` daemon.

1.  **Cloudflare Edge Certificate:** Ensure your domain in Cloudflare is using `Full (Strict)` SSL/TLS encryption mode. This requires a valid certificate on the origin, but Cloudflare Tunnel handles this automatically for the tunnel connection.
2.  **Tunnel Encryption:** The `cloudflared` tunnel inherently uses TLS to encrypt traffic between Cloudflare's edge and the `cloudflared` daemon.
3.  **(Optional) Local TLS:** If you require HTTPS between `cloudflared` and your Home Assistant instance (e.g., `service: https://<home-assistant-ip>:8123`), you need a valid TLS certificate on Home Assistant itself. You can achieve this using:
    *   The Let's Encrypt add-on (if HA is directly accessible for challenges, less common with tunnels).
    *   A self-signed certificate (requires adding `noTLSVerify: true` under the `ingress` rule in `config.yml`, reducing security).
    *   A certificate issued by a private CA.
    *   For most tunnel setups, `service: http://...` is sufficient and secure, as the traffic only travels within your local network unencrypted.

## 4. Implement Security Headers and Protections

Leverage Cloudflare features to enhance security.

1.  **Cloudflare WAF (Web Application Firewall):**
    *   Navigate to `Security` -> `WAF`.
    *   Enable basic rulesets (e.g., Cloudflare Managed Ruleset, OWASP Core Ruleset) to protect against common web vulnerabilities. Start in "Log" or "Simulate" mode to avoid blocking legitimate traffic initially.
2.  **HTTP Strict Transport Security (HSTS):**
    *   Navigate to `SSL/TLS` -> `Edge Certificates`.
    *   Enable HSTS. Configure settings carefully, starting with a short `max-age` and disabling `preload` until you are confident.
3.  **Other Headers (Optional via Transform Rules):**
    *   Consider adding headers like `Content-Security-Policy`, `Permissions-Policy`, `Referrer-Policy` via Cloudflare Transform Rules (`Rules` -> `Transform Rules` -> `Modify Response Header`) for further hardening. This requires careful configuration specific to Home Assistant's needs.

## 5. Configure Mobile App Remote Access

The Home Assistant companion app needs the external URL.

1.  **Open Home Assistant App:** Go to `Settings` -> `Companion App`.
2.  **Server Configuration:**
    *   Ensure your Home Assistant instance details are correct.
    *   Under `Home Network Wi-Fi SSID`, enter the SSIDs of your local network(s).
    *   Under `External URL`, enter your Cloudflare Tunnel URL: `https://ha.yourdomain.com`.
    *   Ensure `Internal URL` points to your local HA address (e.g., `http://<home-assistant-ip>:8123`).
3.  **Test:** Disconnect from Wi-Fi and try accessing Home Assistant via the app. You should be prompted by Cloudflare Access first (if configured), then the Home Assistant login page.

## Security Best Practices

*   **Strong Home Assistant Passwords:** Use unique, strong passwords and enable Multi-Factor Authentication (MFA/2FA) within Home Assistant itself.
*   **Limit Cloudflare Access:** Grant access only to necessary users/groups in Cloudflare Access policies.
*   **Keep Systems Updated:** Regularly update Home Assistant, `cloudflared`, and the underlying operating system.
*   **Monitor Logs:** Check Cloudflare Access logs, WAF logs, and Home Assistant logs for suspicious activity.
*   **Principle of Least Privilege:** Don't run `cloudflared` as root unless necessary for installation/service management.
*   **Firewall:** Although the tunnel bypasses inbound firewall rules for *this specific access*, maintain a restrictive firewall policy on your Home Assistant host for other potential services.

## Testing Procedures

1.  **External Access:** Access `https://ha.yourdomain.com` from a device outside your network.
    *   Verify Cloudflare Access authentication prompt (if enabled).
    *   Verify Home Assistant login page loads.
    *   Verify successful login and functionality.
2.  **TLS Verification:** Use browser developer tools or an online SSL checker to confirm a valid Cloudflare certificate is presented for `ha.yourdomain.com`.
3.  **Mobile App:** Test access via the companion app both on Wi-Fi and cellular data.
4.  **WAF Testing (Simulate/Log Mode):** Attempt common test payloads (e.g., `?test=<script>alert(1)</script>`) to see if they are logged or blocked by the WAF according to your rules.
5.  **Tunnel Stability:** Monitor the `cloudflared` service/process to ensure it remains running. Check Cloudflare Zero Trust dashboard for tunnel status.

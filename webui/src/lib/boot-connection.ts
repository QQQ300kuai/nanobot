import { fetchWsTicket } from "./account-api";
import { deriveWsUrl } from "./bootstrap";
import type { BootstrapResponse } from "./types";

/**
 * The websocket URL and HTTP token for a bootstrap payload. Account gateways hand out a
 * one-shot ticket per connection, so every (re)connect fetches a fresh one.
 */
export async function resolveBootConnection(
  boot: BootstrapResponse,
  accountToken: string,
): Promise<{ url: string; apiToken: string }> {
  if (boot.multi_user && accountToken) {
    const ticket = await fetchWsTicket(accountToken);
    return {
      url: deriveWsUrl(boot.ws_path, ticket, boot.ws_url, "ticket"),
      apiToken: accountToken,
    };
  }
  return {
    url: deriveWsUrl(boot.ws_path, boot.token, boot.ws_url),
    apiToken: boot.api_token ?? "",
  };
}

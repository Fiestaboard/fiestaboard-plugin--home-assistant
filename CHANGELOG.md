# Changelog

## 1.5.0

- Added **Sign in with Home Assistant**, using Home Assistant's built-in login (`/auth/authorize` and `/auth/token` on your Home Assistant address). No client ID or secret to set up.
- A pasted long-lived access token keeps working and is used instead of the sign-in when set. REST and MQTT Statestream modes are unchanged.
- When Home Assistant refuses a signed-in token, the plugin asks FiestaBoard to refresh it once and retries, otherwise the settings show "Reconnect needed".
- The access token is optional in REST mode on FiestaBoard cores that support sign-in.
- Requires FiestaBoard 9.11.0 or later.

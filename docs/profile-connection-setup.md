# Beheerde profielconfiguratie

`hermes_cli.profile_connections` is de Hermes-eigenaar van profielconfiguratie
voor een beoordeeld aansluitpakket. De bestaande geauthenticeerde operatorroute
roept `inspect_profile_connection(request)` of `apply_profile_connection(request)`
aan. Deze functies voeren geen provider-, model-, MCP-subproces- of gatewaystart uit.

Een request heeft `schema_version: hermes.profile_connection_request.v1`,
`action: inspect|apply`, `request_id`, `binding` en `setup_ref`.
`binding` bevat exact `agent_id`, `profile`, `repo`, `workspace`.
`setup_ref` bevat een absoluut, niet via symlinks omgeleid bestandspad `path` en
de `sha256` van de beoordeelde bytes. Alleen `apply` bevat tevens de
`expected_plan_digest` uit de eerdere inspectie. Inspectie schrijft niets.

Het JSON-setupbestand heeft deze vorm:

```json
{
  "schema_version": "hermes.profile_setup.v1",
  "binding": {
    "agent_id": "example-agent",
    "profile": "example-profile",
    "repo": "example-repo",
    "workspace": "/home/david/example-repo"
  },
  "provider": "openai-codex",
  "model": "explicitly-approved-model",
  "soul_ref": {"path": "/reviewed/SOUL.md", "sha256": "<sha256>"},
  "mcp_servers": [
    {"name": "pb", "config_ref": {"path": "/reviewed/pb.json", "sha256": "<sha256>"}}
  ],
  "source_owner": {"repo": "example-repo", "ref": "github:owner/repo@reviewed-sha"},
  "auth_binding": {"kind": "existing_provider_auth", "provider": "openai-codex"}
}
```

Provider en model zijn expliciet beoordeelde configuratiewaarden; er vindt geen
modelkeuze, kostenbesluit of providermigratie plaats. Het MCP-configbestand bevat
een bestaande Hermes-serverdefinitie. `env` en `headers` accepteren uitsluitend
`${ENV_NAME}`-verwijzingen, voor headers eventueel `Bearer ${ENV_NAME}`. De route
accepteert geen inline credentialvelden, tokens in URL's of credentialbestanden.
Het beoordeelde MCP-JSON mag daarnaast `public_env` bevatten voor expliciet
publieke constanten, bijvoorbeeld `PB_GBRAIN_AGENT_ID`, `PB_AGENT_CONNECTIONS_FILE`,
`PB_OOS_PROVIDER_COMMIT` en absolute transportpaden. Deze mapping wordt in `env`
samengevoegd; overlappende namen en herkenbare credentialnamen zoals `*_TOKEN`,
`*_API_KEY` en `*_SECRET` worden geweigerd. De operator beoordeelt dat deze waarden
publiek zijn: de route kan dat niet uit willekeurige bytes afleiden.
De inhoud van mandaat en MCP-definities blijft in de beoordeelde bestanden; de
receipt bevat alleen scopes, namen en hashes. Beoordeling van de uitvoerbare
MCP-definitie blijft vereist: een hash geeft geen uitvoerbevoegdheid.

Een nieuw profiel wordt zonder clone, alias of gebundelde skills gemaakt via de
bestaande profielprimitive. Er wordt geen `.env` of `auth.json` van een ander
profiel gelezen of gekopieerd. De optionele `auth_binding` verwijst uitsluitend
naar de bestaande providerresolver; de resolver wordt hier niet aangeroepen en
kan later ook een centrale credentialroute gebruiken. Zonder binding meldt de
receipt `auth.status: missing` met reden `existing_provider_auth_binding_not_supplied`;
met binding blijft authenticatie `unverified`. Dit is geen uitspraak dat een
profiel zonder eigen authbestand geen centrale authenticatie kan gebruiken.

Eigenaarschap en de lopende/laatste operatie staan onder `connection_setup` in
het bestaande `profile.yaml`. Andere metadata blijft behouden. Bestaande profielen
zonder exact passend eigenaarschap worden als `profile_unmanaged` geïnspecteerd
en nooit overgenomen. Het ingebouwde `default`-profiel kan niet worden beheerd.
Alleen `model.provider`, `model.default`, `terminal.cwd` (exact de gebonden
workspace), `SOUL.md` en de expliciet beheerde MCP-namen worden gewijzigd.
Andere terminalinstellingen blijven behouden. Een update verwijdert alleen eerder beheerde MCP's
die uit het nieuwe pakket ontbreken. Overige configuratie blijft behouden;
envtemplates worden bij writeback niet naar credentialwaarden uitgebreid.

De plandigest bindt de beoordeelde referenties, exacte identiteit, bronhouder en
verwachte beheerde snapshots. Afwijkende snapshots blokkeren vóór toepassing.
Een gedeeltelijke toepassing geeft `configuration_status: incomplete`; dezelfde
request en digest hervatten alleen als ieder veld nog zijn eerdere of bedoelde
waarde heeft. Een andere request kan een lopende operatie niet overnemen.
Succesvolle herlevering wordt teruggelezen zonder opnieuw te configureren.

De versie van de receipt is `hermes.profile_connection_receipt.v1`.
`configuration_status: configured` betekent dat model, mandaat en MCP's werkelijk
en workspace zijn teruggelezen en met het beoordeelde pakket overeenkomen. De algemene status
blijft `incomplete` zolang authenticatie en `runtime_activation: unverified` niet
via hun eigen eigenaargrens zijn bewezen. Configuratie is geen uitgevoerde taak,
PB-handshake of lesgebruik. Deze route wijzigt geen bestaande runtimepin/release.

Uitzetten en annuleren van ongestart werk blijven via `kanban_connections` lopen.
Een profielupdate stopt geen lopende workers of gesprekken en wijzigt geen cron.
Verwijderen van profieldata is geen onderdeel van deze route. De bestaande
destructieve `profile delete`-route heeft een afzonderlijke, expliciete autorisatie.

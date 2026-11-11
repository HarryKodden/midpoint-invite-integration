# TODO — MidPoint-leading Invite

**Branch:** `midpoint-leading`  
**Docs:** see **README.md** and `docs/latex/` (reference implementation).

## Done

- [x] MidPoint Role create → Invite `POST /roles` (`invite_bridge/`)
- [x] MidPoint Role delete → Invite `DELETE /roles/{id}`
- [x] Link stored as MidPoint `extension/inviteRoleId` (`costCenter=invite:{id}` read as legacy fallback)
- [x] SCIM `MIDPOINT_LEADING_GROUPS`: Group create by name; Group DELETE no-op; empty members ignored
- [x] SCIM idempotent User create (`SCIM_IDEMPOTENT_USER_CREATE`)
- [x] Compose + `INVITE_*` / `.env.example` / profile toggle (`.env.invite` | `.test.env`)
- [x] README + `design.md` (Mermaid) + LaTeX operator guide for MidPoint-leading
- [x] Manual `scripts/invite-create-role.sh`
- [x] MidPoint **Invite user** menu link → `invite_bridge` form → Invite `POST /invitations`
- [x] MidPoint admin **unassign Invite-SCIM member** → Invite
      `DELETE /api/external/v1/user_roles/by_provisioned_scim/{userScimId}/{groupScimId}`
      (`invite_bridge` membership snapshot + unit tests + live smoke script)
- [x] Moodle + OpenLDAP removed from default compose (code/objects retained)

## Remaining (Invite core)

- [ ] Unit tests for SCIM leading-mode Group behaviour (sibling `../scim`)

## Parked (not Invite core)

- Moodle / `moodle_bridge` / ConnId Moodle resource
- OpenLDAP / phpLDAPadmin (`ldap/`, MidPoint OpenLDAP post-initial objects)
- OneRoster / NRPS LMS path

## Ops reminders

- Invite must use **remote Manage** (`manage.enabled=true`) or roles show “Onbekend in Manage” and SCIM may not fire.
- `INVITE_APPLICATION_MANAGE_TYPE` must be uppercase (`SAML20_SP`, `OIDC10_RP`, …).
- After changing `ENV_FILE` / `INVITE_*`: recreate `invite_bridge` (and `application` if OIDC differs).
- Cold start of membership snapshot only baselines; it does not mass-revoke.

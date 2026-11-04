# MidPoint ↔ SURF Invite — sequence

MidPoint-leading: MidPoint owns Role create/delete; Invite owns invitation UX;
users and memberships enter MidPoint via SCIM. MidPoint unassign of Invite-SCIM
members notifies Invite (drift prevention).

```mermaid
sequenceDiagram
    actor Admin as MidPoint admin
    actor Inviter as Inviter
    actor Guest as Guest
    participant IDM as MidPoint (IDM/IGA)
    participant Bridge as invite_bridge
    participant Invite as SURF Invite
    participant SCIM as Institutional SCIM
    participant Mail as Mail
    participant Auth as SURFconext / eduID
    participant APP as Application

    Note over IDM,Invite: MidPoint owns roles · Invite owns invites · SCIM carries users/memberships

    rect rgb(245, 248, 252)
    Note over Admin,SCIM: 1. Create role (MidPoint → Invite)
    Admin->>IDM: Create Role
    IDM-->>Admin: Role created
    Bridge->>IDM: Poll / list Roles
    IDM-->>Bridge: New Role
    Bridge->>Invite: POST /api/external/v1/roles
    Invite-->>Bridge: 200 (Invite role id)
    Bridge->>IDM: Store costCenter=invite:{id}
    Invite->>SCIM: POST /Groups (echo)
    SCIM->>IDM: Match Role by name (MIDPOINT_LEADING_GROUPS)
    IDM-->>SCIM: OK
    SCIM-->>Invite: 201 / OK
    end

    rect rgb(245, 252, 248)
    Note over Admin,SCIM: 2. Invite users
    alt From MidPoint GUI (additionalMenuLink)
        Admin->>Bridge: Open "Invite user" form
        Bridge->>Invite: POST /api/external/v1/invitations
    else From Invite application UI
        Inviter->>Invite: Invite users to Role
    end
    Invite->>Mail: Send invitation email(s)
    Mail-->>Guest: Invitation link
    Guest->>Invite: Accept invitation
    Invite->>Auth: Authenticate (eduID)
    Auth-->>Invite: Identity
    Invite->>SCIM: POST /Users (+ idempotent reuse) + PATCH /Groups
    SCIM->>IDM: UserType + Role assignment (+ scimResource)
    IDM-->>SCIM: OK
    SCIM-->>Invite: Success
    end

    rect rgb(252, 248, 245)
    Note over Guest,APP: 3. Sign in (demo app)
    Guest->>APP: Sign in (OIDC via SURFconext)
    APP->>IDM: Lookup user / memberships (or SCIM)
    IDM-->>APP: Groups
    APP-->>Guest: Access if groups match
    end

    rect rgb(252, 245, 248)
    Note over Admin,Invite: 4. Unassign member (drift prevention)
    Admin->>IDM: Unassign Invite-SCIM User from Invite-SCIM Role
    Bridge->>IDM: Poll memberships (snapshot)
    Note right of Bridge: Only Invite-linked + scimResource roles/users
    Bridge->>Invite: DELETE …/by_provisioned_scim/{userScimId}/{groupScimId}
    Invite-->>Bridge: 204 or 404 (idempotent)
    Note right of Bridge: On 5xx: log/alert · MidPoint revoke already done
    end

    rect rgb(248, 248, 248)
    Note over Admin,SCIM: 5. Delete role (MidPoint → Invite)
    Admin->>IDM: Delete Role
    Bridge->>IDM: Poll (Role gone)
    Bridge->>Invite: DELETE /api/external/v1/roles/{id}
    Invite->>SCIM: DELETE /Groups (optional echo)
    SCIM->>IDM: Group DELETE no-op (leading mode)
    end
```

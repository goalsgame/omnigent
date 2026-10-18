"""Safe workspace preparation failures shared by the launcher and host."""

GITHUB_CHECKOUT_UNCONNECTED_EXIT = 81
GITHUB_CHECKOUT_UNCONNECTED = "github_checkout_unconnected"
GITHUB_CHECKOUT_MACHINE_UNAVAILABLE_EXIT = 85
GITHUB_CHECKOUT_MACHINE_UNAVAILABLE = "github_checkout_machine_unavailable"
LORE_ACCESS_UNAVAILABLE_EXIT = 86
LORE_ACCESS_UNAVAILABLE = "lore_access_unavailable"
LORE_CHECKOUT_FAILED_EXIT = 87
LORE_CHECKOUT_FAILED = "lore_checkout_failed"
LORE_WORKSPACE_CONFLICT_EXIT = 88
LORE_WORKSPACE_CONFLICT = "lore_workspace_conflict"
LORE_CLIENT_UNAVAILABLE_EXIT = 89
LORE_CLIENT_UNAVAILABLE = "lore_client_unavailable"
WORKSPACE_ERROR_CODES = {
    GITHUB_CHECKOUT_UNCONNECTED_EXIT: GITHUB_CHECKOUT_UNCONNECTED,
    GITHUB_CHECKOUT_MACHINE_UNAVAILABLE_EXIT: GITHUB_CHECKOUT_MACHINE_UNAVAILABLE,
    LORE_ACCESS_UNAVAILABLE_EXIT: LORE_ACCESS_UNAVAILABLE,
    LORE_CHECKOUT_FAILED_EXIT: LORE_CHECKOUT_FAILED,
    LORE_WORKSPACE_CONFLICT_EXIT: LORE_WORKSPACE_CONFLICT,
    LORE_CLIENT_UNAVAILABLE_EXIT: LORE_CLIENT_UNAVAILABLE,
}
WORKSPACE_ERROR_MESSAGES = {
    LORE_ACCESS_UNAVAILABLE: (
        "Lore access unavailable. Verify the configured repository and renew your Omnigent "
        "sign-in or machine delegation."
    ),
    LORE_CHECKOUT_FAILED: (
        "Lore checkout failed. Verify the repository, revision, view and available workspace "
        "disk space."
    ),
    LORE_WORKSPACE_CONFLICT: (
        "Lore workspace does not match the requested checkout. Existing files were preserved; "
        "start a new session or restore the original repository/ref."
    ),
    LORE_CLIENT_UNAVAILABLE: (
        "Lore CLI is unavailable in the runner image. Ask an administrator to install the "
        "configured Lore client."
    ),
    GITHUB_CHECKOUT_MACHINE_UNAVAILABLE: (
        "GitHub checkout failed: GitHub App access is not authorized for this machine identity. "
        "Ask an administrator to configure github_machine_auth for the bot, "
        "verify its identity is enabled and the App is installed on the repository, "
        "then start a new session."
    ),
    GITHUB_CHECKOUT_UNCONNECTED: (
        "GitHub checkout failed without a connected GitHub account. "
        "For a private repository, connect GitHub in Settings > Integrations. "
        "Check the repository URL and branch, then start a new session."
    ),
}

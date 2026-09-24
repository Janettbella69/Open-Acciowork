# First task setup

FR-36 adds a short setup path to an empty workspace. A new user can choose an
existing example task or write a goal, configure a model in Settings, return
to the workspace, choose a verified model, and send the task. The setup card
disappears once the user has a saved project. The model step reflects the
current provider list: it links to setup when none is runnable, asks the user
to select one when a runnable provider exists, and marks the step ready when
the selected provider is runnable.

The Provider Settings empty state explains where to obtain a standard API
key, that a coding-plan key must not be used, and that the user needs to review
production eligibility and terms, save the key, and run preflight. It links
back to the workspace after configuration. Existing Provider and preflight
gates still decide whether the Run can start.

Local UI tests cover the empty workspace and Provider Settings states. Real
first-time user usability, external Provider onboarding, and browser layout
still need acceptance.

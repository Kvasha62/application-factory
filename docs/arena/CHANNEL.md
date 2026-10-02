# Arena report channel

This branch and its pull request are a permanent, non-merge communication channel between the project owner/ChatGPT and Arena.

## Protocol

- Arena communicates through this pull request: comments, review comments, and PR description.
- Issues are task/context records only; Arena must not be expected to reply there.
- Read-only audits: no repository mutation, commit, push, merge, publication, deployment, or registry mutation.
- Mutation tasks: changes are made on a dedicated `arena/*` branch and reported through the corresponding PR.
- Every task sent to Arena must state:
  - repository and base commit/branch;
  - task type: READ-ONLY or MUTATION;
  - response location;
  - required evidence;
  - constraints / files or systems that must not be touched;
  - known environment blockers.
- Reports must distinguish verified evidence from unavailable checks and must never promote an access-blocked check to PASS.
- This PR is a permanent channel and must not be merged.

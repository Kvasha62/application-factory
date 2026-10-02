# Arena report channel

This branch and its pull request are a permanent, non-merge communication channel between the project owner/ChatGPT and Arena.

## Permanent communication rule

**Canonical rule:** ChatGPT and Arena communicate only through GitHub.

- Every task sent to Arena must be delivered through GitHub.
- Every Arena response must be left on GitHub, using the designated PR response channel.
- For this repository, the permanent Arena response channel is PR #138 (arena/reports).
- ChatGPT must not expect or request an Arena response in this chat, local files, or any other non-GitHub channel.
- Issues may carry task/context information, but Arena must not be expected to reply there because its Issues permission is read-only.
- After asking Arena to perform a task, ChatGPT must read and verify the resulting response from GitHub before treating the task as completed.

## Protocol

- Arena communicates through this pull request: comments, review comments, and PR description.
- Issues are task/context records only; Arena must not be expected to reply there.
- Read-only audits: no repository mutation, commit, push, merge, publication, deployment, or registry mutation.
- Mutation tasks: changes are made on a dedicated arena/* branch and reported through the corresponding PR.
- Every task sent to Arena must state:
  - repository and base commit/branch;
  - task type: READ-ONLY or MUTATION;
  - response location;
  - required evidence;
  - constraints / files or systems that must not be touched;
  - known environment blockers.
- Reports must distinguish verified evidence from unavailable checks and must never promote an access-blocked check to PASS.
- This PR is a permanent channel and must not be merged.

## Verified interaction test

The GitHub-only protocol was verified end-to-end on 2026-10-02:

- Test task was posted to PR #138 as a READ-ONLY request.
- Arena replied with a top-level PR comment from arena-ai-coding-agent[bot].
- Arena confirmed PR #138 as its permanent response channel.
- Arena reported head SHA b40947bf05f583bcf6dfcebf821c4dfcfad7094f.
- GitHub API independently confirmed the same arena/reports head SHA.
- PR #138 was independently confirmed OPEN, DRAFT, and not merged.
- No repository mutation was performed by Arena during the test.
- Issue #137 was not used as the response channel.

This verified interaction is the operational reference for applying the rule in future tasks.

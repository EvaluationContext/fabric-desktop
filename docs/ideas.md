# Submitting ideas

Fabric Desktop is shaped by the people who use it. Ideas are collected in one public place, the **[Ideas forum](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/ideas)** in this repository's GitHub Discussions. Everyone can see every idea, vote for the ones they want, and follow what happens to them.

This page explains how to submit a good idea, how voting works, and how ideas are prioritized and turned into features.

| You want to | Go to |
| --- | --- |
| Suggest a feature or a change | [Ideas forum](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/ideas) (this page) |
| Report something that's broken | **Report a problem** in the app (account menu), or a [bug report](https://github.com/KratosDataGod/fabric-desktop/issues/new/choose) |
| Ask how to do something | [Q&A](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/q-a) |
| Contribute code or docs | [CONTRIBUTING.md](https://github.com/KratosDataGod/fabric-desktop/blob/main/CONTRIBUTING.md) |

## Before you post

1. **Search first.** Use the search box on the [Ideas](https://github.com/KratosDataGod/fabric-desktop/discussions/categories/ideas) page with a few words about the problem. With thousands of users, your idea may already be there.
2. **If it's there, vote and add your case.** Click the **upvote arrow** on the idea, and comment only if you have something new: a different situation, a number ("we'd save 3 hours a week"), or a constraint nobody mentioned. A comment that just says "+1" doesn't count for more than the vote.
3. **Keep it public-safe.** The forum is public. Don't post tenant names, workspace or item IDs, data, connection strings, or screenshots with data in them. Describe the shape of the problem instead.

You need a free GitHub account to post or vote. If your organization prefers, your Fabric Desktop platform team can collect ideas internally and post them for you; say how many people asked in the post.

## Posting an idea

Choose **New discussion** in the **Ideas** category. The form asks for:

| Field | What makes it useful |
| --- | --- |
| **Title** | The problem in a few words, not the solution. "Can't see which tables a promote will change" beats "Add a diff button". |
| **The problem** | What you were trying to do, what got in the way, and what you do today instead. |
| **Who it affects** | Your role (data engineer, analyst, admin, team lead) and roughly how many people. |
| **How often and how much** | Daily, weekly, rarely. Time lost, risk, or cost. |
| **Area of the app** | So it reaches the right people. |
| **Your idea** (optional) | How you imagine it working. Maintainers may solve the problem another way, so the problem matters more. |
| **Would you try an early version?** | People who tick this may be asked to test a Preview build. |

One idea per post. If you have three, post three, so each can be voted on.

## How voting works

- **One person, one vote** per idea: the upvote arrow at the top of the post. You can take your vote back.
- **Votes are a signal, not a ballot.** They show how many people share the problem. They feed into prioritization below, together with impact and effort, so a small, high-impact fix can go ahead of a popular large one.
- **Comments with your situation count.** A clear description of a case nobody else mentioned often matters more than another vote.
- **Sort by Top** on the Ideas page to see what's most wanted.

## How ideas are prioritized

Maintainers review the forum on a regular rhythm.

**Every week: triage new ideas.** Each new idea gets one of:

| Outcome | What you see |
| --- | --- |
| Needs more detail | A comment asking a question. Ideas left without an answer for a month may be closed. |
| Duplicate | Closed as **Duplicate**, with a link to the idea to vote on instead. |
| Out of scope | Closed with a comment explaining why (see [What's out of scope](#whats-out-of-scope)). |
| Open for votes | Stays open. Most ideas end up here. |

**Every month: pick what's next.** Open ideas are scored on four things:

| Factor | Question | How it's judged |
| --- | --- | --- |
| **Reach** | How many people have this problem? | Votes, plus people counted in comments and posts made on behalf of a team |
| **Impact** | How much does it help each of them? | 3 = removes a blocker or a real risk; 2 = saves real time every week; 1 = nice to have |
| **Confidence** | Do we understand the problem and know it can be built on Fabric's APIs? | High, medium or low |
| **Effort** | How big is it? | Small (days), medium (a couple of weeks), large (more) |

Roughly, **Reach × Impact × Confidence ÷ Effort**. It's a guide, not a formula that decides alone. Maintainers also weigh:

- **Safety and correctness first.** Anything that could lose data, write a bad table, or leak access jumps the queue.
- **Fabric's direction.** If Microsoft is about to ship the same thing in Fabric, we'd rather work with it than duplicate it.
- **Keeping the app simple.** New features go into pages people already use; the left rail doesn't grow for every idea ([UI guidelines](ui-guidelines.md)).

**What happens to a chosen idea:**

1. A maintainer comments **Planned** on the discussion and links a GitHub issue. Follow that issue for progress.
2. When it's built, it ships to the **Preview** channel first and is tagged **Preview** in the app until it has been tried on a real tenant. People who ticked "Would you try an early version?" may be asked to.
3. When it reaches **Stable**, the discussion is closed as **Resolved**, with the build and a link to its docs. It also appears in **What's new** in the app.

Ideas that aren't chosen stay open and keep collecting votes. Nothing is lost by waiting; the score is re-checked every month.

## What's out of scope

Some things have been looked at and decided against, so ideas about them will be closed with a pointer here:

- **A phone or mobile app.** Fabric Desktop is a desktop and server tool.
- **Replacing Fabric.** Fabric Desktop runs data engineering work locally and stores everything in Fabric. Features that only make sense without Fabric (a different storage layer, a different reporting tool) don't fit.
- **Storing secrets.** Passwords and keys stay in Azure Key Vault; the app only holds a vault URL and secret name.

## For maintainers

- **Turn on Discussions once:** repository **Settings > General > Features > Discussions**. GitHub creates the default categories, including **Ideas** and **Q&A**. The Ideas form in `.github/DISCUSSION_TEMPLATE/ideas.yml` is used automatically, because its file name matches the category.
- **Weekly triage** takes about 15 minutes: sort Ideas by **Newest**, apply the outcomes above, and link duplicates.
- **Monthly prioritization:** sort by **Top**, score the leading ideas, comment **Planned** with a linked issue on the ones chosen, and add those issues to the next milestone.
- **Close the loop:** close as **Resolved** when an idea reaches Stable, and write its line in the **Promote to stable** notes so it shows in What's new.
- **Pin** a discussion each quarter that lists what was planned and shipped from the forum, so people can see their votes matter.
- Labels are optional. The repository's default labels (`enhancement`, `duplicate`, `question`, `wontfix`) are enough; add area labels (such as `area: notebooks`, `area: metadata`) if the forum grows.

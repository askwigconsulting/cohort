---
name: broken-yaml
kind: agent
scope: global
description: The frontmatter block is never terminated.
targets: [all]
department: IT
advisory: true

This file has no closing '---', so frontmatter parsing must fail with E001.

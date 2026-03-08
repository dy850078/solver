# CLAUDE.md - solver

> **Documentation Version**: 1.0
> **Last Updated**: 2026-03-08
> **Project**: solver
> **Description**: Python-based VM placement optimizer that uses Google OR-Tools CP-SAT solver to find the best assignment of Kubernetes cluster VMs to baremetal servers, replacing the existing round-robin approach in Go scheduler. Runs as a sidecar service (HTTP or CLI) that receives VM requirements and baremetal capacity from the Go scheduler, and returns an optimized placement plan that respects capacity limits, candidate filtering, and AG-based anti-affinity spreading.
> **Features**: GitHub auto-backup, technical debt prevention

This file provides essential guidance to Claude Code when working with code in this repository.

## CRITICAL RULES - READ FIRST

### RULE ACKNOWLEDGMENT REQUIRED
Before starting ANY task, Claude Code must respond with:
"CRITICAL RULES ACKNOWLEDGED - I will follow all prohibitions and requirements listed in CLAUDE.md"

### ABSOLUTE PROHIBITIONS
- NEVER create new files in root directory -> use proper module structure
- NEVER write output files directly to root directory -> use output/
- NEVER create documentation files (.md) unless explicitly requested by user
- NEVER use git commands with -i flag (interactive mode not supported)
- NEVER use `find`, `grep`, `cat`, `head`, `tail`, `ls` commands -> use Read, Glob, Grep tools instead
- NEVER create duplicate files (manager_v2.py, enhanced_xyz.py, utils_new.py) -> ALWAYS extend existing files
- NEVER create multiple implementations of same concept -> single source of truth
- NEVER copy-paste code blocks -> extract into shared utilities/functions
- NEVER hardcode values that should be configurable -> use config files/environment variables
- NEVER use naming like enhanced_, improved_, new_, v2_ -> extend original files instead

### MANDATORY REQUIREMENTS
- COMMIT after every completed task/phase - no exceptions
- GITHUB BACKUP - Push to GitHub after every commit: `git push origin main`
- USE TASK AGENTS for all long-running operations (>30 seconds)
- TODOWRITE for complex tasks (3+ steps) -> parallel agents -> git checkpoints -> test validation
- READ FILES FIRST before editing
- DEBT PREVENTION - Before creating new files, check for existing similar functionality
- SINGLE SOURCE OF TRUTH - One authoritative implementation per feature/concept

### EXECUTION PATTERNS
- PARALLEL TASK AGENTS - Launch multiple Task agents simultaneously for maximum efficiency
- SYSTEMATIC WORKFLOW - TodoWrite -> Parallel agents -> Git checkpoints -> GitHub backup -> Test validation
- GITHUB BACKUP WORKFLOW - After every commit: `git push origin main`

### MANDATORY PRE-TASK COMPLIANCE CHECK
Before starting any task, verify ALL points:

**Step 1: Rule Acknowledgment**
- [ ] I acknowledge all critical rules in CLAUDE.md and will follow them

**Step 2: Task Analysis**
- [ ] Will this create files in root? -> If YES, use proper module structure instead
- [ ] Will this take >30 seconds? -> If YES, use Task agents not Bash
- [ ] Is this 3+ steps? -> If YES, use TodoWrite breakdown first
- [ ] Am I about to use grep/find/cat? -> If YES, use proper tools instead

**Step 3: Technical Debt Prevention (MANDATORY SEARCH FIRST)**
- [ ] SEARCH FIRST: Use Grep to find existing implementations
- [ ] CHECK EXISTING: Read any found files to understand current functionality
- [ ] Does similar functionality already exist? -> If YES, extend existing code
- [ ] Am I creating a duplicate class/manager? -> If YES, consolidate instead
- [ ] Will this create multiple sources of truth? -> If YES, redesign approach

**Step 4: Session Management**
- [ ] Is this a long/complex task? -> If YES, plan context checkpoints

## PROJECT OVERVIEW

solver is a Python-based VM placement optimizer using Google OR-Tools CP-SAT solver. It finds the optimal assignment of Kubernetes cluster VMs to baremetal servers as a sidecar service to the Go scheduler.

### Key Components
- `solver.py` - Core CP-SAT optimization logic
- `server.py` - HTTP sidecar service entry point
- `models.py` - Data models and entities
- `serialization.py` - Request/response serialization
- `tests/` - Test suite
- `docs/` - API, user, and developer documentation
- `examples/` - Usage examples and sample requests

### Project Structure
```
solver/
├── CLAUDE.md
├── README.md
├── .gitignore
├── requirements.txt
├── solver.py              # Core CP-SAT solver
├── server.py              # HTTP sidecar server
├── models.py              # Data models
├── serialization.py       # Serialization utilities
├── src/
│   ├── main/
│   │   ├── python/
│   │   │   ├── core/      # Core business logic
│   │   │   ├── utils/     # Utility functions
│   │   │   ├── models/    # Extended data models
│   │   │   ├── services/  # Service layer
│   │   │   └── api/       # API endpoints
│   │   └── resources/
│   │       ├── config/    # Configuration files
│   │       └── assets/    # Static assets
│   └── test/
│       ├── unit/
│       └── integration/
├── tests/                 # Main test suite
├── docs/                  # Documentation
├── examples/              # Usage examples
├── tools/                 # Dev tools and scripts
└── output/                # Generated output files
```

### Development Status
- **Setup**: Complete
- **Core Solver**: In progress
- **HTTP Service**: In progress
- **Testing**: In progress
- **Documentation**: In progress

## GITHUB BACKUP WORKFLOW

```bash
# After every commit, always run:
git push origin main
```

## COMMON COMMANDS

```bash
# Install dependencies
pip install -r requirements.txt

# Run the solver (CLI mode)
python solver.py

# Run the HTTP server
python server.py

# Run tests
python -m pytest tests/

# Push backup to GitHub
git push origin main
```

## TECHNICAL DEBT PREVENTION

### WRONG APPROACH:
```bash
# Creating new file without searching first
Write(file_path="new_feature.py", content="...")
```

### CORRECT APPROACH:
```bash
# 1. SEARCH FIRST
Grep(pattern="feature.*implementation", include="*.py")
# 2. READ EXISTING FILES
Read(file_path="existing_feature.py")
# 3. EXTEND EXISTING FUNCTIONALITY
Edit(file_path="existing_feature.py", old_string="...", new_string="...")
```

## RULE COMPLIANCE CHECK

Before starting ANY task, verify:
- [ ] I acknowledge all critical rules above
- [ ] Files go in proper module structure (not root)
- [ ] Use Task agents for >30 second operations
- [ ] TodoWrite for 3+ step tasks
- [ ] Commit after each completed task

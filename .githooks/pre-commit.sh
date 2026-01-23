#!/bin/bash

# Disable pagers
export PAGER=cat
export GIT_PAGER=cat

# Colors for output
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

EXIT_CODE=0
FIXES_APPLIED=0

echo -e "${BLUE}=== Running Pre-commit Checks ===${NC}"

# Check formatting
echo -e "${BLUE}Checking code formatting...${NC}"
if ! (uv run ruff format --check . 2>/dev/null); then
    echo -e "${YELLOW}Formatting code...${NC}"
    uv run ruff format .
    FIXES_APPLIED=1
else
    echo -e "${GREEN}✓ Code is properly formatted${NC}"
fi

# Run linting
echo -e "${BLUE}Running linting...${NC}"
RUFF_OUTPUT=$(uv run ruff check --fix --exit-non-zero-on-fix . 2>&1)
RUFF_EXIT=$?

if [ $RUFF_EXIT -eq 0 ]; then
    echo -e "${GREEN}✓ No linting issues found${NC}"
else
    echo "$RUFF_OUTPUT"
    # Extract error count (macOS compatible)
    REMAINING=$(echo "$RUFF_OUTPUT" | grep -o 'Found [0-9]* error' | grep -o '[0-9]*' | tail -1)
    if [ -z "$REMAINING" ] || [ "$REMAINING" = "0" ]; then
        echo -e "${GREEN}✓ Automatic fixes applied${NC}"
        FIXES_APPLIED=1
    else
        echo -e "${RED}✗ Found $REMAINING linting issue(s) that need manual attention${NC}"
        EXIT_CODE=1
    fi
fi

# Run tests
echo -e "${BLUE}Running tests...${NC}"
if uv run pytest -q 2>/dev/null; then
    echo -e "${GREEN}✓ All tests passed${NC}"
else
    echo -e "${RED}✗ Tests failed${NC}"
    EXIT_CODE=1
fi

# Check merge conflicts
echo -e "${BLUE}Checking for merge conflict markers...${NC}"
if git grep -I --line-number '^<<<<<<< ' -- '*.py' 2>/dev/null; then
    echo -e "${RED}Error: Found merge conflict markers${NC}"
    EXIT_CODE=1
fi

# Final exit logic
if [ $FIXES_APPLIED -eq 1 ]; then
    echo ""
    echo -e "${YELLOW}✓ Automatic fixes have been applied. Please review and commit again.${NC}"
    exit 1
fi

if [ $EXIT_CODE -eq 1 ]; then
    echo ""
    echo -e "${RED}✗ Pre-commit check failed. Please fix the issues above.${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Pre-commit checks passed - code is clean!${NC}"

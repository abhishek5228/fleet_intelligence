# Contributing to Fleet Intelligence

Thanks for wanting to help! Here is how to contribute.

## Setup
1. Fork this repo and clone your fork.
2. Backend:
   cd backend
   pip install -r requirements.txt -r requirements-dev.txt
   uvicorn main:app --reload
3. Frontend:
   cd frontend
   npm install
   npm start

API keys (Gemini, Maps, WhatsApp) are optional. Without them
the app runs in a safe simulated mode.

## Making a change
1. Create a branch: git checkout -b fix/short-description
2. Make your change.
3. Run the tests: cd backend && pytest tests/ -v
4. Commit and push your branch.
5. Open a Pull Request and explain what you changed and why.
   If it fixes an issue, write "Fixes #number".

## Rules
- Never commit API keys, tokens or real phone numbers.
- Keep PRs small and focused on one thing.
- Be kind and respectful to everyone.

## Where to start
Look for issues labeled "good first issue".

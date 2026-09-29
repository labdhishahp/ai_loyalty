Vercel serves this directory as the site's static assets.

It is deliberately empty. Without an explicit outputDirectory, Vercel finds no
framework at the repository root and falls back to serving the project folder
itself -- which published every Python package (core/, agent/, llm/, ...) plus
pyproject.toml and requirements.txt as downloadable files. This API has no
static assets; the only public surface is /api/* and /mcp/*.

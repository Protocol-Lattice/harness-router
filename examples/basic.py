import asyncio

from harness_router import (
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
)


async def main() -> None:
    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(provider, RoutingConfig())

    tools = [
        ToolDescriptor(
            name="read_file",
            description="Read a repository file by path",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="search_code",
            description="Search source code for a symbol or text",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="write_file",
            description="Replace a repository file with new content",
            category="mutate",
            risk=RiskLevel.MEDIUM,
        ),
    ]

    try:
        decision = await router.route(
            HarnessState(
                goal="Fix the failing parser test",
                observation="The failing assertion references src/parser.py",
            ),
            tools,
        )
        print(decision)
    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())

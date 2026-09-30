(() => {
  'use strict';

  const scenarios = {
    inspect: {
      tool: 'read_file',
      confidence: '0.93',
      observation: 'The failing test points to src/parser.py.',
    },
    patch: {
      tool: 'write_file',
      confidence: '0.91',
      observation: 'The parser drops the final token. The fix is identified.',
    },
    verify: {
      tool: 'run_tests',
      confidence: '0.96',
      observation: 'The parser fix is in place. Check it against the tests.',
    },
  };

  document.querySelectorAll('[data-scenario]').forEach((button) => {
    button.addEventListener('click', () => {
      const scenario = scenarios[button.dataset.scenario];
      if (!scenario) return;
      document.querySelectorAll('[data-scenario]').forEach((control) => {
        control.setAttribute('aria-pressed', String(control === button));
      });
      document.querySelectorAll('[data-candidate]').forEach((candidate) => {
        candidate.classList.toggle('is-selected', candidate.dataset.candidate === scenario.tool);
      });
      document.querySelectorAll('[data-route]').forEach((route) => {
        route.classList.toggle('is-selected', route.dataset.route === scenario.tool);
      });
      document.getElementById('demo-observation').textContent = scenario.observation;
      document.getElementById('demo-tool').textContent = scenario.tool;
      document.getElementById('demo-confidence').textContent = scenario.confidence;
    });
  });

  const repository = 'https://github.com/Protocol-Lattice/harness-router/blob/main/';
  const rawRepository = 'https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/';
  const providers = {
    codex: {
      label: 'Codex', requirement: 'Git project root',
      title: 'Start a fresh Codex session',
      description: 'The startup hook discovers tools. Prompt and result callbacks then prepare the next choice for local validation.',
      guide: 'README.md#integrations',
    },
    claude: {
      label: 'Claude Code', requirement: 'Project root',
      title: 'Start a fresh Claude Code session',
      description: 'Session startup discovers native and configured MCP tools. Prompt and tool-result hooks prepare choices for the next step.',
      guide: '.claude/README.md',
    },
    ohmypi: {
      label: 'ohmypi', requirement: 'Project root · Bun',
      title: 'Run omp from your project root',
      description: 'The extension loads from .omp/extensions and routes using the live tool registry. The installer uses Bun to install dependencies and check its TypeScript types.',
      guide: '.omp/README.md',
    },
    antigravity: {
      label: 'Antigravity', requirement: 'Project root',
      title: 'Restart your Antigravity session',
      description: 'Refresh the project hooks and MCP server. Invocation and result callbacks prepare decisions; PreToolUse validates the stored choice.',
      guide: '.antigravity/README.md',
    },
    deepseek: {
      label: 'DeepSeek Harness', requirement: 'Project root',
      title: 'Start DeepSeek with the hook patch',
      description: 'Supply the actual tools in .dsh/harness-router-tools.json and make goal context available. Start with: dsh --patch .dsh/harness-router.patch.yml',
      guide: 'hooks/deepseek/README.md',
    },
  };

  const providerInputs = [...document.querySelectorAll('input[name="hook-provider"]')];
  function selectProvider(name) {
    const provider = providers[name];
    if (!provider) return;
    providerInputs.forEach((input) => { input.checked = input.value === name; });
    document.getElementById('provider-name').textContent = provider.label;
    document.getElementById('hook-install-requirements').textContent = provider.requirement;
    document.getElementById('hook-install-command').textContent =
      `curl -fsSL ${rawRepository}scripts/install_hook.py \\\n  | python3 - --provider ${name}`;
    document.getElementById('hook-install-copy').setAttribute('aria-label', `Copy ${provider.label} hook installation command`);
    document.getElementById('provider-next-title').textContent = provider.title;
    document.getElementById('provider-next-text').textContent = provider.description;
    document.getElementById('provider-guide').href = repository + provider.guide;
    const needsAdapters = name === 'codex' || name === 'claude';
    document.getElementById('adapter-completion').hidden = !needsAdapters;
    if (needsAdapters) {
      document.getElementById('adapter-command').textContent =
        `for hook in pre_decision stop; do\n  curl -fsSL "${rawRepository}.${name}/hooks/\${hook}.py" \\\n    -o ".${name}/hooks/\${hook}.py"\ndone`;
    }
  }
  providerInputs.forEach((input) => input.addEventListener('change', () => selectProvider(input.value)));
  document.querySelectorAll('[data-select-provider]').forEach((link) => {
    link.addEventListener('click', () => selectProvider(link.dataset.selectProvider));
  });
  selectProvider(providerInputs.find((input) => input.checked)?.value || 'codex');

  function legacyCopy(text) {
    if (typeof document.execCommand !== 'function') return false;
    const previousFocus = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;opacity:0;inset:0;pointer-events:none;';
    document.body.append(field);
    let copied = false;
    try {
      field.select();
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    } finally {
      field.remove();
      if (previousFocus instanceof HTMLElement) previousFocus.focus({ preventScroll: true });
    }
    return copied;
  }

  document.querySelectorAll('[data-copy-target]').forEach((button) => {
    let resetTimer;
    button.addEventListener('click', async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      const text = target.textContent;
      let copied = false;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
          copied = true;
        }
      } catch {
        copied = false;
      }
      if (!copied) copied = legacyCopy(text);
      clearTimeout(resetTimer);
      button.textContent = copied ? 'Copied ✓' : 'Try again';
      button.classList.toggle('is-copied', copied);
      document.getElementById('copy-status').textContent = copied
        ? 'Command copied to clipboard.'
        : 'Copy failed. Select the command to copy it manually.';
      resetTimer = setTimeout(() => {
        button.textContent = 'Copy ⧉';
        button.classList.remove('is-copied');
      }, 1800);
    });
  });

  const header = document.querySelector('.site-header');
  const menuButton = document.querySelector('.menu-toggle');
  function setMenu(open) {
    header.classList.toggle('menu-open', open);
    menuButton.setAttribute('aria-expanded', String(open));
    menuButton.querySelector('span').textContent = open ? '−' : '+';
  }
  menuButton.addEventListener('click', () => {
    setMenu(menuButton.getAttribute('aria-expanded') !== 'true');
  });
  document.querySelectorAll('#site-nav a').forEach((link) => {
    link.addEventListener('click', () => setMenu(false));
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menuButton.getAttribute('aria-expanded') === 'true') {
      setMenu(false);
      menuButton.focus();
    }
  });
})();

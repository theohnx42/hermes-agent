import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { test } from 'vitest'

import {
  detectRemotePlatform,
  readRemoteProfileInventory,
  readWindowsRemoteProfileInventory
} from './windows-remote-lifecycle'

function windowsSsh(payload, fail?) {
  const calls: { command: string; options: any }[] = []

  return {
    calls,
    async exec(command, options?) {
      calls.push({ command, options })

      if (command === 'uname -s; uname -m') {throw new Error('uname unavailable')}

      if (fail) {throw fail}

      return `\uFEFF#< CLIXML <Objs S="progress"></Objs>${JSON.stringify(payload)}#< CLIXML <Objs></Objs>`
    }
  }
}

test('Windows inventory selects metadata without requiring any executable and normalizes names/identity', async () => {
  const ssh = windowsSsh({
    os: 'Windows',
    profiles: ['reviewer', 'default', '.hidden', 'bad name', 'reviewer'],
    installId: `\uFEFF${'AB'.repeat(16)}\r\n`
  })

  assert.deepEqual(await readRemoteProfileInventory(ssh, 'C:\\missing\\hermes.exe'), {
    os: 'Windows',
    profiles: ['default', 'reviewer'],
    installId: 'ab'.repeat(16)
  })
  const script = Buffer.from(ssh.calls[1].options.stdinData.trim(), 'base64').toString('utf16le')
  assert.match(script, /Get-ChildItem -LiteralPath \$profilesDir -Directory/)
  assert.match(script, /Assert-NoReparse \$entry.FullName/)
  assert.match(script, /Assert-NoReparse \$identity/)
  assert.match(script, /Split-Path -Leaf \$parent/)
  assert.doesNotMatch(script, /Get-Command|python|hermes\.exe|Start-Process|New-Item|WriteAllText/)
})

for (const profiles of [[], ['only']])
  {test(`Windows inventory preserves array shape for ${profiles.length} directories and leaves absent identity absent`, async () => {
    assert.deepEqual(await readWindowsRemoteProfileInventory(windowsSsh({ os: 'Windows', profiles, installId: '' })), {
      os: 'Windows',
      profiles: ['default', ...profiles],
      installId: undefined
    })
  })}

test('malformed Windows metadata and unsafe-path errors do not fall back to a runtime', async () => {
  await assert.rejects(
    () => readWindowsRemoteProfileInventory(windowsSsh({ os: 'Windows', profiles: 'single', installId: '' })),
    /metadata JSON/
  )
  const ssh = windowsSsh(null, new Error('Path contains a link or reparse point'))
  await assert.rejects(() => readRemoteProfileInventory(ssh), /reparse point/)
  assert.equal(ssh.calls.length, 2)

  const result = await readWindowsRemoteProfileInventory(
    windowsSsh({ os: 'Windows', profiles: [], installId: '../../invalid' })
  )

  assert.equal(result.installId, undefined)
})

test('platform transport refusal retains its type and never enters Windows inventory', async () => {
  const cause = Object.assign(new Error('host key rejected'), { kind: 'host-key-changed' })

  const ssh = {
    exec: async () => {
      throw cause
    }
  }

  await assert.rejects(
    () => readRemoteProfileInventory(ssh),
    error => error === cause
  )
})

test('default Windows platform detection retains executable probe behavior', async () => {
  const ssh = windowsSsh({
    os: 'Windows',
    arch: 'AMD64',
    hermesPath: 'C:\\custom\\hermes.exe',
    python: 'C:\\custom\\python.exe'
  })

  await detectRemotePlatform(ssh, 'C:\\custom\\hermes.exe')
  const script = Buffer.from(ssh.calls[1].options.stdinData.trim(), 'base64').toString('utf16le')
  assert.match(script, /\$explicit='C:\\custom\\hermes.exe'/)
  assert.match(script, /Get-Command hermes.exe/)
})

test('POSIX inventory preserves its existing validation and install identity reads', async () => {
  const commands: string[] = []

  const ssh = {
    async exec(command) {
      commands.push(command)

      if (command === 'uname -s; uname -m') {return 'Linux\nx86_64'}

      if (command.includes('ls -1')) {return 'coder\nreviewer\n'}

      if (command.includes('cat ')) {return 'ab'.repeat(16)}

      return '/home/owner/.hermes'
    }
  }

  assert.deepEqual(await readRemoteProfileInventory(ssh), {
    profiles: ['default', 'coder', 'reviewer'],
    installId: 'ab'.repeat(16)
  })
  assert.ok(commands.some(command => command.includes('ls -1')))
  assert.ok(commands.every(command => !command.includes('powershell')))
})

// Exercise the real Electron inventory consumer without booting Electron or opening a tunnel.
test('Electron inventory publishes Windows profiles and shared identity, and closes its read-only session', async () => {
  const source = readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

  const body = source
    .slice(
      source.indexOf('async function probeSshProfileInventory('),
      source.indexOf('async function enumerateRegistryAgentSources(')
    )
    .replace('catch (error: any)', 'catch (error)')

  const roster = new Map(),
    identities = new Map(),
    logs: string[] = []

  let opened = 0,
    closed = 0,
    configuredPath = ''

  const ssh = {
    open: async () => {
      opened++
    },
    close: async () => {
      closed++
    }
  }

  const dependencies = {
    shouldRetrySshInventory: () => true,
    sshRosterCache: roster,
    sshInventoryAttemptedAt: new Map(),
    SSH_INVENTORY_RETRY_MS: 60000,
    normalizeSshConfig: value => value,
    createSshProbeConnection: () => ssh,
    sshRememberLog: value => logs.push(value),
    desktopSshBinary: () => 'ssh',
    connectionInstallIds: identities,
    remoteLifecycle: {
      listRemoteHermesProfiles: () => {
        throw new Error('Unsafe remote Hermes home.')
      },
      readRemoteInstallId: () => {
        throw new Error('POSIX identity read on Windows')
      }
    },
    readRemoteProfileInventory: async (connection, explicit) => {
      assert.equal(connection, ssh)
      configuredPath = explicit

      return { profiles: ['default', 'condor'], installId: 'ab'.repeat(16) }
    }
  }

  const probe = new Function(...Object.keys(dependencies), `${body}; return probeSshProfileInventory`)(
    ...Object.values(dependencies)
  )

  await probe({ id: 'nightfall', host: 'node', remoteHermesPath: 'C:\\custom\\hermes.exe' })
  assert.deepEqual(roster.get('nightfall'), ['default', 'condor'])
  assert.equal(identities.get('nightfall').id, 'ab'.repeat(16))
  assert.equal(configuredPath, 'C:\\custom\\hermes.exe')
  assert.deepEqual([opened, closed, logs.length], [1, 1, 0])
})

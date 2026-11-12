import type { EngineInterface, Register } from 'claude-code'

const USAGE = 'Use /portavoce topic, /portavoce topic new or /portavoce test.'
const WORDS = ['topic', 'topic new', 'test']

let topicOption = ''
let tokenOption = ''

// The script reads these options from its environment, as it does when a hook runs it.
const scriptEnv = () => ({
  ...(topicOption === '' ? {} : { CLAUDE_PLUGIN_OPTION_NTFY_TOPIC: topicOption }),
  ...(tokenOption === '' ? {} : { CLAUDE_PLUGIN_OPTION_NTFY_TOKEN: tokenOption }),
})

const portavoce = ($: EngineInterface, args: string[]) =>
  $.process.run(['python3', `${$.plugin.root}/hooks/limit_restore_notify.py`, ...args], { env: scriptEnv() })

export const register: Register = (on, options) => {
  topicOption = String(options.ntfy_topic ?? '').trim()
  tokenOption = String(options.ntfy_token ?? '').trim()

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'portavoce',
      description: 'Show the ntfy topic to subscribe the phone to, or send a test push',
      argumentHint: 'topic|topic new|test',
    })
    return next(e)
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'portavoce' }, async ($, e) => {
    const words = e.args.trim().split(/\s+/).filter(Boolean)
    if (!WORDS.includes(words.join(' '))) return { text: USAGE }
    return { text: (await portavoce($, words)).stdout.trim() }
  })
}

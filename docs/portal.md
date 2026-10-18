# Using Kelvin

The portal is where you chat with Kelvin: the desktop app's window, or the site that `kelvin` opens in your browser.

## In the portal

- **Models:** Add keys under Settings for Claude, GPT or Gemini, or point it at a local OpenAI-compatible server (Ollama, vLLM, LM Studio) for confidential work. Keys stay in `~/Kelvin/settings.json` and go only to their own provider.
- **ChatGPT plan:** Instead of an OpenAI API key, you can use your ChatGPT plan. In Settings press **Continue with ChatGPT** and sign in on OpenAI's page in your browser.
  - **What Kelvin gets:** it never sees your password and gets no access to your ChatGPT conversations.
  - **Where it shows:** ChatGPT models then appear as **ChatGPT plan** in the model picker, and **Using ChatGPT plan** shows by the picker while one is selected.
  - **Tokens:** they're kept in `~/Kelvin/chatgpt.json`, renewed automatically, and revoked when you sign out.
  - **Limits:** use **Manage usage** to set them; project rules can allow or exclude ChatGPT plan by name.
  - **Basis:** this follows OpenAI's Sign in with ChatGPT for open-source and personal local apps.
- **Other model services:** Under Settings → **Other model services**, add any service that speaks the OpenAI Chat Completions API.
  - **Ready-made:** OpenRouter (models from many companies with one key), OpenCode, DeepSeek, Groq, Mistral and xAI. Pick one and paste its key.
  - **Anything else:** choose **Other** and give its address (for example Together, Fireworks, or a LiteLLM gateway).
  - **How each appears:** each service shows under its own name in the model picker, so a cloud service is never counted as a local model. A project's rules can allow or exclude it by name.
  - **Which models:** Kelvin lists the newest models that can call tools (it needs tools), or the ones you name.
  - **OpenCode:** only its Chat Completions models are listed, because its Claude, GPT and Gemini models use other formats.
  - **Security:** keys can also come from environment variables such as `OPENROUTER_API_KEY`. Services on the internet need https.
- **Projects:** The sidebar groups conversations into projects, for example one per sample or experiment. Make a project with **+**, start a chat inside it with its **+**, and move a conversation with its **⋯** menu or by dragging it onto a project. Deleting a project keeps its conversations.
- **Memory:** Kelvin learns from conversations, but keeps three levels that never mix:
  - **You, in every chat:** your instructions and the preferences and conventions Kelvin kept, such as units or how careful to be (Settings → Memory).
  - **One project:** that project's instructions and notes, such as decisions, procedures, instrument quirks and results (the project's **⋯ → Project settings**). Only chats in that project see them. Deleting a project deletes its memory.
  - **The conversation itself:** a new chat never sees another chat's history.

  Kelvin keeps short, typed notes, not transcripts, and never questions or chit-chat. It merges a fact it already knows instead of saving it twice. Recalled notes come with your message, and a reply says which ones it used ("Recalled 2 notes"). A large project recalls only the notes that bear on the question. You can delete any note, or switch off **Learn from conversations**.
- **Confidential projects:** In a project's **Project settings → Who may see this project**, choose which kinds of model may read it (Claude, GPT, Gemini, local models), and whether literature search may be used. For confidential work, allow only local models and switch off literature search.
  - **Enforcement:** Kelvin refuses any other model for chats, comparisons and second opinions, and leaves out the plan reviewer if it isn't allowed. The composer shows **Confidential**.
  - **The rules stay with the chat:** a chat that ran under them keeps them, even if it is moved out of the project or the project is deleted, so its history never reaches a model you excluded.
- **Incognito:** For a question that has nothing to do with your work, press **Incognito** in the top bar; from a saved chat it starts a new incognito one. Kelvin then uses no memory and keeps none, and the chat is never saved or listed; it is gone when you leave it. Instrument commands and runs are still logged, for safety.
- **Literature:** Kelvin searches arXiv and journal papers (through [OpenAlex](https://openalex.org)) and reads them, so it can plan an experiment from what is known and check a result against published work. Try "what parameters did people use to drive magnons with SAWs in CrSBr? propose a plan".
  - **Search:** results come with authors, venue, citation counts and abstracts.
  - **Reading:** arXiv papers with an HTML version are read in full, section by section, with equations as LaTeX; other papers give their abstract.
  - **Citations:** Kelvin cites what it used as [Author Year](link) and never invents a reference.
  - **Reading list:** in a project, ask Kelvin to keep a paper. It goes on the project's reading list (Project settings), Kelvin sees that list in the project's conversations, and reports list it as references.
  - **Privacy:** only the search words go to arXiv and OpenAlex, never your data or chats. Switch literature search off in Settings for confidential work. A free OpenAlex API key (Settings) raises the daily search allowance.
- **Several models at once:** Press **Compare** next to the model picker and tick one or two more models. Each message then goes to all of them, and the answers stream side by side. While comparing, the models can read the lab (status, values, runs, dry runs) but not change it, so three models never drive the same instruments. Press **Continue with this answer** under the one you prefer; the conversation goes on from that answer and with that model.
- **Second opinions:** A different model checks Kelvin's work.
  - **Plans:** pick a reviewer in Settings → **Second opinion on plans**. It then reads every plan before it runs: the request, the lab's state, Kelvin's own checks, and the plan. It answers go, caution or stop with its reasons, shown under the plan's step. If it says stop, Kelvin asks you before running the plan.
  - **Answers:** press **Second opinion** under the latest answer and choose a model. It says whether it agrees, partly agrees or disagrees, and why, and Kelvin sees that opinion with your next message.
  - **Limits:** the reviewer never gets tools, so it can't touch the lab. Its advice doesn't replace the safety gate's limits or approvals. A model from another company gives a more independent view.
- **Conversation:** Answers stream in Markdown. Each tool call is a step you can expand, and anything that drives the sample harder appears as an **Approve / Decline** card inside the conversation.
- **Runs pane:** Measurements get their own pane beside the chat, like a terminal pane, with one tab per run. It opens when a plan starts, including plans started by another agent. While the plan runs, the pane shows a progress bar, the points done, the time left and plots that grow point by point, plus a Stop button. When the run ends, Kelvin analyses the data. The conversation gets a short card with the assessment and the next step. The pane shows the key results with uncertainties, the plots with their fits (hover for values), the table and the plan. Drag the pane's edge to resize it; open earlier runs from its clock button.
- **Lab panel:** Shows live instrument values (drives away from safe are highlighted) and the activity log.
- **Safety:** Kelvin uses the same MCP tools and safety gate as external agents, so it has the same rules and no extra power. Only one conversation drives the lab at a time; Stop ends the current answer and any ramp.

## Terminal

The **Terminal** button in the top bar (or Ctrl+`) opens a console at the foot of the window. There you can type to Kelvin, or send an instrument its own commands.
- **Talking to Kelvin:** type as in a chat. Kelvin's steps show as `●` lines with their results below, a run's progress counts up, and its data appear live in the Runs pane. The conversation is saved like any other chat.
- **An instrument's own commands:** `/use lockin` (or just `/lockin`) sends what you type to that instrument, such as `*IDN?`, `SENS?` or `:SOUR:VOLT 1`. Queries show the reply; anything else shows what it did. `/kelvin` goes back to talking to Kelvin.
- **The same safety gate:** limits, interlocks and blocked commands apply as for Kelvin. A change that drives the sample harder asks **approve? [y/N]** right in the terminal. Moving back toward safe doesn't ask. A terminal command never lands in the middle of a run: while Kelvin is busy it is refused, not queued. Every command is in the lab record as yours (`human (terminal)`), and Kelvin's **undo** never undoes it.
- **Other commands:**
  - `/ls`: the instruments.
  - `/status [name]`: readings, outputs and settings.
  - `/get X`, `/set time_constant 300 ms`, `/set smu.V 0.5`: read or set by name.
  - `/describe [name]`: units, limits and safe values.
  - `/manual <words>`: search the instrument's manual.
  - `/new`: start a new conversation with Kelvin.
  - `/stop`: stop Kelvin's answer and any run.
  - `/clear`: clear the screen.
- **Keys:** Up and Down recall what you typed. Tab completes commands, instrument names and parameters. Esc stops Kelvin's answer.

## Phone and remote access

Check on a run from the coffee room, or answer an approval from home. Phone access is **off** until you switch it on in Settings → **Phone access**. When it's on, Kelvin also listens on the network (port 8771 by default). Your desktop's access key never works there; only paired phones do.

1. Switch on **Phone access**. Kelvin shows its addresses: on this network, and on Tailscale if you use it.
2. Name the phone, choose what it may do, and press **Pair a phone**.
   - **Watch:** see the lab, runs and alarms, stop a run, and decline requests.
   - **Control:** also chat with Kelvin and approve requests.
3. On the phone, scan the QR code, or open the address and type the 6-digit code. The code works once, for 10 minutes, and repeated wrong codes lock pairing for a while.

The phone then opens Kelvin's phone page: alarms with **Got it**, requests with **Approve** (control) or **Decline**, the run in progress with its progress and **Stop**, the instruments, and recent notices. A control phone can also open the full portal.

Settings, instruments, the lab config, the approval mode and pairing work only on the Kelvin computer. Remove a phone in Settings, or with **Forget this phone** on the phone itself.

**Away from the lab:** install [Tailscale](https://tailscale.com) (a free, encrypted VPN) on the lab computer and on the phone, and pair the phone using the Tailscale address. On a plain Wi-Fi network traffic isn't encrypted, so on a shared network prefer Tailscale. Don't forward Kelvin's port to the internet. For push notifications to the phone, add an ntfy topic under Notifications.

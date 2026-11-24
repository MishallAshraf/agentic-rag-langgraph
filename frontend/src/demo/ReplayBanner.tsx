import { recordedAt } from './replay'

/** Says plainly that this is a recording, and where the live project lives. */
export default function ReplayBanner() {
  return (
    <div className="replay-banner" role="note">
      <span>
        <strong>Replay demo.</strong>
        <span className="long"> The real frontend, playing back answers recorded from the running
        app on {recordedAt}. Every step and source is real; nothing is generated live.</span>
        <span className="short"> Real answers recorded on {recordedAt}.</span>
      </span>
      <a href="https://mishallashraf.github.io/">← Mishal Ashraf</a>
    </div>
  )
}

// The avatars a bot may choose (bot_config.AVATARS), as simple line icons.
const PATHS: Record<string, string> = {
  bot: 'M12 4v3M7 7h10a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3v-6a3 3 0 0 1 3-3zM9 12h.01M15 12h.01M9.5 16h5',
  owl: 'M6 5l2 2h8l2-2v9a6 6 0 0 1-12 0zM10 11a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM17 11a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM11 14l1 1 1-1',
  book: 'M4 5a2 2 0 0 1 2-2h5v17H6a2 2 0 0 0-2 1zM20 5a2 2 0 0 0-2-2h-5v17h5a2 2 0 0 1 2 1z',
  compass: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM15.5 8.5l-2 5-5 2 2-5z',
  leaf: 'M5 19C5 10 10 5 20 4c-1 10-6 15-15 15zM5 19l7-7',
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6',
  shield: 'M12 3l7 3v5c0 5-3 8.5-7 10-4-1.5-7-5-7-10V6zM9 12l2 2 4-4',
  chat: 'M5 5h14a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-8l-4 3v-3H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z',
}

export function Avatar({ name, size = 40 }: { name: string; size?: number }) {
  return (
    <span className="avatar" style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox="0 0 24 24" width={size * 0.6} height={size * 0.6} fill="none" stroke="currentColor"
           strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d={PATHS[name] ?? PATHS.bot} />
      </svg>
    </span>
  )
}

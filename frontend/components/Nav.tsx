'use client';

/**
 * Left-hand page navigation.
 *
 * Two pages, and the pairing is intentional. **Suggestions** reports conclusions; **Chart** shows the
 * data those conclusions were drawn from. Every panel on the suggestions page looks the same whether
 * the prices behind it are current, stale or absent — the chart is the one view where that is
 * obvious at a glance, which is why it exists.
 */
import Link from 'next/link';
import { usePathname } from 'next/navigation';

const PAGES = [
  { href: '/', label: 'Suggestions', hint: 'what the strategies currently say' },
  { href: '/chart', label: 'Chart', hint: 'the prices behind it' },
  { href: '/data', label: 'Data', hint: 'is every source still arriving?' },
  { href: '/analytics', label: 'Analytics', hint: 'what has been suggested, and how it ended' },
  { href: '/specs', label: 'Specs', hint: 'where each strategy is defined' },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="nav" aria-label="Pages">
      <div className="nav-brand">
        crypto&#8209;trader
        <span className="nav-brand-sub">read&#8209;only</span>
      </div>
      <ul>
        {PAGES.map((p) => {
          const active = path === p.href;
          return (
            <li key={p.href}>
              <Link href={p.href} className={active ? 'nav-link active' : 'nav-link'}
                    aria-current={active ? 'page' : undefined}>
                <span>{p.label}</span>
                <span className="nav-hint">{p.hint}</span>
              </Link>
            </li>
          );
        })}
      </ul>
      <p className="nav-foot">
        This system never places, cancels or transfers anything. Nothing here has been validated
        against history.
      </p>
    </nav>
  );
}

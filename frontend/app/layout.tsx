import type { Metadata } from 'next';

import { Nav } from '@/components/Nav';
import './globals.css';

export const metadata: Metadata = {
  title: 'Trade suggestions — read only',
  description:
    'Suggestions for a person to act on. This system never places, cancels or transfers anything.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Nav />
          <div className="content">{children}</div>
        </div>
      </body>
    </html>
  );
}

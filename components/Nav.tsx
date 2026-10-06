"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Investigate" },
  { href: "/proposals", label: "Proposals" },
  { href: "/audit", label: "Audit" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav className="row">
      {LINKS.map((link) => {
        const active =
          link.href === "/" ? path === "/" : path.startsWith(link.href);
        return (
          <Link key={link.href} href={link.href} className={active ? "on" : ""}>
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}

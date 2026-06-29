'use client';

import { ChevronRight, Home } from 'lucide-react';
import Link from 'next/link';

export interface BreadcrumbItem {
    label: string;
    href?: string;
}

interface BreadcrumbsProps {
    items: BreadcrumbItem[];
}

export function Breadcrumbs({ items }: BreadcrumbsProps) {
    return (
        <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm">
            <Link
                href="/overview"
                className="flex items-center justify-center w-8 h-8 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-all duration-200"
            >
                <Home className="w-4 h-4" />
            </Link>
            {items.map((item, index) => (
                <span key={index} className="flex items-center gap-1.5 group">
                    <ChevronRight className="w-4 h-4 text-muted-foreground/50" />
                    {item.href ? (
                        <Link
                            href={item.href}
                            className="px-2.5 py-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-all duration-200 font-medium"
                        >
                            {item.label}
                        </Link>
                    ) : (
                        <span className="px-2.5 py-1 text-foreground font-semibold bg-primary/10 rounded-lg">
                            {item.label}
                        </span>
                    )}
                </span>
            ))}
        </nav>
    );
}

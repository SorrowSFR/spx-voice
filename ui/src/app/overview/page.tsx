"use client";

import {
    Activity,
    ArrowRight,
    Bot,
    Clock,
    Key,
    Phone,
    Sparkles,
    TrendingUp,
    Workflow,
    Zap,
} from 'lucide-react';
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { getWorkflowsApiV1WorkflowFetchGet } from '@/client/sdk.gen';
import { Button } from '@/components/ui/button';
import {
    Card,
    CardContent,
    CardDescription,
    CardHeader,
    CardTitle,
} from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { type LocalUser, useAuth } from '@/lib/auth';

export default function OverviewPage() {
    const { user, provider } = useAuth();
    const [workflows, setWorkflows] = useState<Array<{ id: number; name: string; created_at: string }>>([]);
    const [loading, setLoading] = useState(true);
    const hasFetched = useRef(false);

    const canManagePlatform = provider === 'local'
        ? Boolean((user as LocalUser | undefined)?.is_superuser)
        : true;

    useEffect(() => {
        if (hasFetched.current) return;
        hasFetched.current = true;

        const fetchWorkflows = async () => {
            try {
                const response = await getWorkflowsApiV1WorkflowFetchGet({});
                if (response.data) {
                    const data = response.data as unknown as { workflows?: Array<{ id: number; name: string; created_at: string }> };
                    setWorkflows(data?.workflows?.slice(0, 5) ?? []);
                }
            } catch (error) {
                console.error('Failed to fetch workflows:', error);
            } finally {
                setLoading(false);
            }
        };

        fetchWorkflows();
    }, []);

    const StatCard = ({
        icon: Icon,
        label,
        value,
        subtext,
        gradient,
    }: {
        icon: typeof Phone;
        label: string;
        value: string | number;
        subtext?: string;
        gradient?: string;
    }) => (
        <Card className="card-hover relative overflow-hidden">
            {/* Gradient accent */}
            <div className={cn(
                "absolute top-0 right-0 w-32 h-32 rounded-full blur-3xl opacity-10",
                gradient
            )} />
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">{label}</CardTitle>
                <div className={cn(
                    "p-2.5 rounded-xl shadow-sm",
                    gradient
                )}>
                    <Icon className="h-4 w-4 text-white" />
                </div>
            </CardHeader>
            <CardContent>
                <div className="text-3xl font-bold tracking-tight">
                    {loading ? <Skeleton className="h-9 w-20" /> : value}
                </div>
                {subtext && <p className="text-xs text-muted-foreground mt-1.5 font-medium">{subtext}</p>}
            </CardContent>
        </Card>
    );

    return (
        <div className="container mx-auto px-4 py-8 space-y-10">
            {/* Hero Welcome Section */}
            <div className="relative animate-fade-in-up">
                {/* Decorative elements */}
                <div className="absolute -top-20 -right-20 w-72 h-72 bg-gradient-to-br from-blue-500/10 to-purple-500/10 rounded-full blur-3xl" />
                <div className="absolute -bottom-10 -left-10 w-48 h-48 bg-gradient-to-br from-cyan-500/10 to-blue-500/10 rounded-full blur-3xl" />

                <div className="relative">
                    <div className="flex items-center gap-2 mb-3">
                        <Sparkles className="h-5 w-5 text-amber-500" />
                        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Dashboard</span>
                    </div>
                    <h1 className="text-4xl font-bold tracking-tight mb-3">
                        {user?.displayName ? (
                            <span>Welcome back, <span className="text-gradient">{user.displayName.split(' ')[0]}</span></span>
                        ) : 'Welcome to Voice Console'}
                    </h1>
                    <p className="text-lg text-muted-foreground max-w-2xl leading-relaxed">
                        {canManagePlatform
                            ? "Build intelligent voice agents, manage telephony, and monitor performance — all from one powerful console."
                            : "View usage, reports, and billing for your managed voice agent."}
                    </p>
                </div>
            </div>

            {/* Quick Stats */}
            {canManagePlatform && (
                <>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5 stagger-children">
                        <StatCard
                            icon={Bot}
                            label="Voice Agents"
                            value={loading ? "..." : workflows.length}
                            subtext="Total agents deployed"
                            gradient="bg-gradient-to-br from-blue-500 to-blue-600"
                        />
                        <StatCard
                            icon={Zap}
                            label="Quick Start"
                            value="Voice"
                            subtext="Powered by WebRTC"
                            gradient="bg-gradient-to-br from-amber-500 to-orange-500"
                        />
                        <StatCard
                            icon={TrendingUp}
                            label="Analytics"
                            value="Reports"
                            subtext="View performance"
                            gradient="bg-gradient-to-br from-emerald-500 to-teal-500"
                        />
                        <StatCard
                            icon={Phone}
                            label="Telephony"
                            value="Setup"
                            subtext="Configure providers"
                            gradient="bg-gradient-to-br from-purple-500 to-violet-500"
                        />
                    </div>

                    {/* Recent Workflows */}
                    {workflows.length > 0 && (
                        <Card className="card-hover">
                            <CardHeader>
                                <div className="flex items-center justify-between">
                                    <div>
                                        <CardTitle className="text-xl">Recent Agents</CardTitle>
                                        <CardDescription>Your most recently created voice agents</CardDescription>
                                    </div>
                                    <Link href="/workflow" className="hidden sm:block">
                                        <Button variant="ghost" size="sm" className="gap-2 group">
                                            View all
                                            <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                        </Button>
                                    </Link>
                                </div>
                            </CardHeader>
                            <CardContent>
                                <div className="space-y-3">
                                    {workflows.slice(0, 5).map((workflow, index) => (
                                        <Link
                                            key={workflow.id}
                                            href={`/workflow/${workflow.id}`}
                                            className="group flex items-center justify-between p-4 rounded-xl border border-border/50 hover:bg-accent/50 transition-all duration-200 hover:border-primary/20"
                                            style={{ animationDelay: `${index * 80}ms` }}
                                        >
                                            <div className="flex items-center gap-4">
                                                <div className="bg-gradient-to-br from-blue-500 to-blue-600 p-3 rounded-xl shadow-lg shadow-blue-500/20 group-hover:scale-105 transition-transform">
                                                    <Bot className="h-5 w-5 text-white" />
                                                </div>
                                                <div>
                                                    <p className="font-semibold text-base group-hover:text-primary transition-colors">{workflow.name}</p>
                                                    <p className="text-xs text-muted-foreground mt-0.5">
                                                        Created {new Date(workflow.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                                                    </p>
                                                </div>
                                            </div>
                                            <Button variant="ghost" size="sm" className="gap-2 group-hover:gap-3 transition-all">
                                                Open
                                                <ArrowRight className="h-4 w-4" />
                                            </Button>
                                        </Link>
                                    ))}
                                </div>
                            </CardContent>
                        </Card>
                    )}

                    {/* Quick Actions - Premium Grid */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                        <Card className="card-hover overflow-hidden">
                            <CardHeader className="pb-3">
                                <div className="flex items-center gap-3">
                                    <div className="bg-gradient-to-br from-blue-500 to-cyan-500 p-2.5 rounded-xl shadow-lg shadow-blue-500/20">
                                        <Workflow className="h-5 w-5 text-white" />
                                    </div>
                                    <div>
                                        <CardTitle>Build & Configure</CardTitle>
                                        <CardDescription>Create and manage voice agents</CardDescription>
                                    </div>
                                </div>
                            </CardHeader>
                            <CardContent className="space-y-2">
                                <Link href="/workflow" className="block">
                                    <Button variant="outline" className="w-full justify-between group">
                                        <span className="flex items-center gap-2">
                                            <Bot className="h-4 w-4" />
                                            Go to Agents
                                        </span>
                                        <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                    </Button>
                                </Link>
                                <Link href="/model-configurations" className="block">
                                    <Button variant="outline" className="w-full justify-between group">
                                        <span className="flex items-center gap-2">
                                            <Activity className="h-4 w-4" />
                                            Configure Models
                                        </span>
                                        <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                    </Button>
                                </Link>
                                <Link href="/telephony-configurations" className="block">
                                    <Button variant="outline" className="w-full justify-between group">
                                        <span className="flex items-center gap-2">
                                            <Phone className="h-4 w-4" />
                                            Telephony Settings
                                        </span>
                                        <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                    </Button>
                                </Link>
                            </CardContent>
                        </Card>

                        <Card className="card-hover overflow-hidden">
                            <CardHeader className="pb-3">
                                <div className="flex items-center gap-3">
                                    <div className="bg-gradient-to-br from-emerald-500 to-teal-500 p-2.5 rounded-xl shadow-lg shadow-emerald-500/20">
                                        <TrendingUp className="h-5 w-5 text-white" />
                                    </div>
                                    <div>
                                        <CardTitle>Monitor & Analyze</CardTitle>
                                        <CardDescription>Track performance metrics</CardDescription>
                                    </div>
                                </div>
                            </CardHeader>
                            <CardContent className="space-y-2">
                                <Link href="/usage" className="block">
                                    <Button variant="outline" className="w-full justify-between group">
                                        <span className="flex items-center gap-2">
                                            <TrendingUp className="h-4 w-4" />
                                            View Usage
                                        </span>
                                        <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                    </Button>
                                </Link>
                                <Link href="/reports" className="block">
                                    <Button variant="outline" className="w-full justify-between group">
                                        <span className="flex items-center gap-2">
                                            <Clock className="h-4 w-4" />
                                            Daily Reports
                                        </span>
                                        <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                    </Button>
                                </Link>
                            </CardContent>
                        </Card>
                    </div>
                </>
            )}

            {/* Managed User View */}
            {!canManagePlatform && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <Card className="card-hover overflow-hidden">
                        <CardHeader className="pb-3">
                            <div className="flex items-center gap-3">
                                <div className="bg-gradient-to-br from-blue-500 to-cyan-500 p-2.5 rounded-xl shadow-lg">
                                    <TrendingUp className="h-5 w-5 text-white" />
                                </div>
                                <div>
                                    <CardTitle>Usage</CardTitle>
                                    <CardDescription>Review call activity and usage</CardDescription>
                                </div>
                            </div>
                        </CardHeader>
                        <CardContent>
                            <Link href="/usage">
                                <Button className="gap-2 group">
                                    <TrendingUp className="h-4 w-4" />
                                    View Usage
                                    <ArrowRight className="h-4 w-4 group-hover:translate-x-1 transition-transform" />
                                </Button>
                            </Link>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* Resources Section */}
            {canManagePlatform && (
                <Card className="card-hover">
                    <CardHeader>
                        <CardTitle>Platform Resources</CardTitle>
                        <CardDescription>
                            Additional tools and settings
                        </CardDescription>
                    </CardHeader>
                    <CardContent>
                        <div className="flex flex-wrap gap-3">
                            <Link href="/settings">
                                <Button variant="outline" size="sm" className="gap-2">
                                    <Settings className="h-4 w-4" />
                                    Settings
                                </Button>
                            </Link>
                            <Link href="/reports">
                                <Button variant="outline" size="sm" className="gap-2">
                                    <FileText className="h-4 w-4" />
                                    Reports
                                </Button>
                            </Link>
                            <Link href="/api-keys">
                                <Button variant="outline" size="sm" className="gap-2">
                                    <Key className="h-4 w-4" />
                                    API Keys
                                </Button>
                            </Link>
                            <Link href="/files">
                                <Button variant="outline" size="sm" className="gap-2">
                                    <Database className="h-4 w-4" />
                                    Files
                                </Button>
                            </Link>
                        </div>
                    </CardContent>
                </Card>
            )}
        </div>
    );
}

// Import Settings for resources section
import { Database,FileText, Settings } from 'lucide-react';

import { cn } from '@/lib/utils';

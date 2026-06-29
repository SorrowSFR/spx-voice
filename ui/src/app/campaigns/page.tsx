"use client";

import { Megaphone,Plus } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import { getCampaignsApiV1CampaignGet } from '@/client/sdk.gen';
import type { CampaignsResponse } from '@/client/types.gen';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table';
import { useAuth } from '@/lib/auth';

export default function CampaignsPage() {
    const { user, getAccessToken, redirectToLogin, loading } = useAuth();
    const router = useRouter();

    const [campaignsData, setCampaignsData] = useState<CampaignsResponse | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const hasFetched = useRef(false);

    // Redirect if not authenticated
    useEffect(() => {
        if (!loading && !user) {
            redirectToLogin();
        }
    }, [loading, user, redirectToLogin]);

    // Fetch campaigns once when user is ready
    useEffect(() => {
        if (loading || !user || hasFetched.current) {
            return;
        }
        hasFetched.current = true;

        const fetchCampaigns = async () => {
            setIsLoading(true);
            try {
                const accessToken = await getAccessToken();
                const response = await getCampaignsApiV1CampaignGet({
                    headers: {
                        'Authorization': `Bearer ${accessToken}`,
                    }
                });

                if (response.data) {
                    setCampaignsData(response.data);
                }
            } catch (error) {
                console.error('Failed to fetch campaigns:', error);
            } finally {
                setIsLoading(false);
            }
        };

        fetchCampaigns();
    }, [loading, user, getAccessToken]);

    const handleRowClick = (campaignId: number) => {
        router.push(`/campaigns/${campaignId}`);
    };

    const handleCreateCampaign = () => {
        router.push('/campaigns/new');
    };

    const formatDate = (dateString: string) => {
        return new Date(dateString).toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        });
    };

    const getStateBadgeVariant = (state: string) => {
        switch (state) {
            case 'created':
                return 'secondary';
            case 'running':
                return 'success';
            case 'paused':
                return 'warning';
            case 'completed':
                return 'info';
            case 'failed':
                return 'destructive';
            default:
                return 'secondary';
        }
    };

    return (
        <div className="container mx-auto px-4 py-8 space-y-8">
            {/* Premium Header */}
            <div className="relative animate-fade-in-up">
                <div className="absolute -top-10 -right-10 w-64 h-64 bg-gradient-to-br from-purple-500/5 to-pink-500/5 rounded-full blur-3xl -z-10" />

                <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
                    <div>
                        <div className="flex items-center gap-2 mb-2">
                            <div className="h-1.5 w-8 bg-gradient-to-r from-purple-500 to-pink-500 rounded-full" />
                            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Campaigns</span>
                        </div>
                        <h1 className="text-3xl font-bold tracking-tight">Bulk Campaigns</h1>
                        <p className="text-muted-foreground mt-1">Manage bulk workflow execution campaigns</p>
                    </div>
                    <Button onClick={handleCreateCampaign} className="gap-2 group">
                        <Plus className="h-4 w-4" />
                        Create Campaign
                    </Button>
                </div>
            </div>

            <Card className="card-hover overflow-hidden">
                <CardHeader className="pb-4">
                    <div className="flex items-center gap-3">
                        <div className="bg-gradient-to-br from-purple-500 to-pink-500 p-2.5 rounded-xl shadow-lg shadow-purple-500/20">
                            <Megaphone className="h-5 w-5 text-white" />
                        </div>
                        <div>
                            <CardTitle className="text-xl">All Campaigns</CardTitle>
                            <CardDescription>
                                View and manage your bulk execution campaigns
                            </CardDescription>
                        </div>
                    </div>
                </CardHeader>
                <CardContent>
                    {isLoading ? (
                        <div className="animate-pulse space-y-4">
                            {[...Array(5)].map((_, i) => (
                                <div key={i} className="h-16 bg-muted/50 rounded-xl"></div>
                            ))}
                        </div>
                    ) : campaignsData && campaignsData.campaigns.length > 0 ? (
                        <div className="overflow-x-auto rounded-xl border">
                            <Table>
                                <TableHeader>
                                    <TableRow className="bg-muted/30 hover:bg-muted/30">
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground">ID</TableHead>
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground">Name</TableHead>
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground">Workflow</TableHead>
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground">State</TableHead>
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground text-center">Progress</TableHead>
                                        <TableHead className="text-xs uppercase tracking-wider font-semibold text-muted-foreground">Created</TableHead>
                                        <TableHead className="text-right text-xs uppercase tracking-wider font-semibold text-muted-foreground">Action</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {campaignsData.campaigns.map((campaign) => (
                                        <TableRow
                                            key={campaign.id}
                                            className="group cursor-pointer transition-all duration-200 hover:bg-accent/40"
                                            onClick={() => handleRowClick(campaign.id)}
                                        >
                                            <TableCell className="font-mono text-sm text-muted-foreground">
                                                #{campaign.id}
                                            </TableCell>
                                            <TableCell className="font-semibold">
                                                {campaign.name}
                                            </TableCell>
                                            <TableCell className="text-muted-foreground">
                                                {campaign.workflow_name}
                                            </TableCell>
                                            <TableCell>
                                                <Badge variant={getStateBadgeVariant(campaign.state)}>
                                                    {campaign.state}
                                                </Badge>
                                            </TableCell>
                                            <TableCell className="text-center">
                                                <span className="inline-flex items-center justify-center px-3 py-1.5 text-sm font-bold bg-gradient-to-r from-purple-500/10 to-pink-500/10 rounded-full border border-purple-500/20">
                                                    {campaign.executed_count} / {campaign.total_queued_count}
                                                </span>
                                            </TableCell>
                                            <TableCell className="text-sm text-muted-foreground">
                                                {formatDate(campaign.created_at)}
                                            </TableCell>
                                            <TableCell className="text-right">
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    onClick={(e) => { e.stopPropagation(); handleRowClick(campaign.id); }}
                                                    className="group-hover:bg-primary group-hover:text-primary-foreground transition-colors"
                                                >
                                                    View
                                                </Button>
                                            </TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>
                    ) : (
                        <div className="text-center py-12 rounded-xl border-2 border-dashed bg-muted/20">
                            <Megaphone className="h-12 w-12 mx-auto text-muted-foreground/50 mb-4" />
                            <p className="text-lg font-medium mb-2">No campaigns yet</p>
                            <p className="text-muted-foreground mb-4">Create your first bulk campaign to get started</p>
                            <Button onClick={handleCreateCampaign} variant="outline" className="gap-2">
                                <Plus className="h-4 w-4" />
                                Create your first campaign
                            </Button>
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}

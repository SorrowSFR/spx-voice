'use client';

import { Archive, ArrowRight, Bot,Pencil, RotateCcw } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useState, useTransition } from 'react';
import { toast } from 'sonner';

import { updateWorkflowStatusApiV1WorkflowWorkflowIdStatusPut } from '@/client/sdk.gen';
import { Button } from '@/components/ui/button';
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
interface Workflow {
    id: number;
    name: string;
    status: string;
    created_at: string;
    total_runs?: number | null;
}

interface WorkflowTableProps {
    workflows: Workflow[];
    showArchived: boolean;
}

export function WorkflowTable({ workflows, showArchived }: WorkflowTableProps) {
    const router = useRouter();
    const [isPending, startTransition] = useTransition();
    const [loadingWorkflowId, setLoadingWorkflowId] = useState<number | null>(null);

    const handleEdit = (id: number) => {
        router.push(`/workflow/${id}`);
    };

    const handleArchiveToggle = async (id: number, currentStatus: string) => {
        const newStatus = currentStatus === 'active' ? 'archived' : 'active';
        const action = currentStatus === 'active' ? 'Archive' : 'Restore';

        setLoadingWorkflowId(id);

        try {
            const response = await updateWorkflowStatusApiV1WorkflowWorkflowIdStatusPut({
                path: {
                    workflow_id: id,
                },
                body: {
                    status: newStatus,
                },
            });

            if (response.data) {
                toast.success(`Workflow ${action.toLowerCase()}d successfully`);
                startTransition(() => {
                    router.refresh();
                });
            }
        } catch (error) {
            console.error(`Error ${action.toLowerCase()}ing workflow:`, error);
            toast.error(`Failed to ${action.toLowerCase()} workflow`);
        } finally {
            setLoadingWorkflowId(null);
        }
    };

    return (
        <div className="bg-card border rounded-xl overflow-hidden shadow-sm">
            <Table>
                <TableHeader>
                    <TableRow className="hover:bg-transparent border-b">
                        <TableHead className="font-semibold text-muted-foreground text-xs uppercase tracking-wider">ID</TableHead>
                        <TableHead className="font-semibold text-muted-foreground text-xs uppercase tracking-wider">Agent Name</TableHead>
                        <TableHead className="font-semibold text-muted-foreground text-xs uppercase tracking-wider">Created At</TableHead>
                        <TableHead className="font-semibold text-muted-foreground text-xs uppercase tracking-wider text-center">Total Runs</TableHead>
                        <TableHead className="font-semibold text-muted-foreground text-xs uppercase tracking-wider text-right">Actions</TableHead>
                    </TableRow>
                </TableHeader>
                <TableBody>
                    {workflows.map((workflow, index) => (
                        <TableRow
                            key={workflow.id}
                            className={`group border-b-0 last:border-0 hover:bg-accent/30 transition-all duration-200 ${showArchived ? 'opacity-60' : ''}`}
                            style={{ animationDelay: `${index * 60}ms` }}
                        >
                            <TableCell className="text-muted-foreground font-mono text-sm">
                                #{workflow.id}
                            </TableCell>
                            <TableCell className="font-medium">
                                <div className="flex items-center gap-3">
                                    <div className="bg-gradient-to-br from-blue-500 to-blue-600 p-2 rounded-lg shadow-sm group-hover:scale-105 transition-transform">
                                        <Bot className="h-4 w-4 text-white" />
                                    </div>
                                    <span className="group-hover:text-primary transition-colors">{workflow.name}</span>
                                </div>
                            </TableCell>
                            <TableCell className="text-sm text-muted-foreground">
                                {new Date(workflow.created_at).toLocaleDateString('en-US', {
                                    year: 'numeric',
                                    month: 'short',
                                    day: 'numeric',
                                })}
                            </TableCell>
                            <TableCell className="text-center">
                                <span className="inline-flex items-center justify-center min-w-[2.5rem] px-3 py-1.5 text-sm font-bold bg-gradient-to-r from-blue-500/10 to-cyan-500/10 text-foreground rounded-full border border-blue-500/20">
                                    {workflow.total_runs || 0}
                                </span>
                            </TableCell>
                            <TableCell className="text-right">
                                <div className="flex justify-end gap-2">
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => handleEdit(workflow.id)}
                                        className="gap-2 hover:gap-3 transition-all group/btn"
                                    >
                                        <Pencil size={15} />
                                        <span>Edit</span>
                                        <ArrowRight size={15} className="opacity-0 group-hover/btn:opacity-100 -ml-1" />
                                    </Button>
                                    <Button
                                        variant={showArchived ? "default" : "outline"}
                                        size="sm"
                                        onClick={() => handleArchiveToggle(workflow.id, workflow.status)}
                                        disabled={loadingWorkflowId === workflow.id || isPending}
                                        className="gap-2 transition-all"
                                    >
                                        {loadingWorkflowId === workflow.id ? (
                                            <>
                                                <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                                                {showArchived ? 'Restoring...' : 'Archiving...'}
                                            </>
                                        ) : (
                                            <>
                                                {showArchived ? (
                                                    <>
                                                        <RotateCcw size={15} />
                                                        Restore
                                                    </>
                                                ) : (
                                                    <>
                                                        <Archive size={15} />
                                                        Archive
                                                    </>
                                                )}
                                            </>
                                        )}
                                    </Button>
                                </div>
                            </TableCell>
                        </TableRow>
                    ))}
                </TableBody>
            </Table>
        </div>
    );
}

from dashboard.models import GapAnalysisMetric

def calculate_gap_metric_display_properties(metric: GapAnalysisMetric):
    """
    Calculates and sets display properties (gap_percent, priority_class,
    gap_class) for a given GapAnalysisMetric instance.
    """
    current = metric.current
    target = metric.target
    
    if target > 0:
        if metric.metric == 'CAC Payback Period':
            # For payback period, a lower number is better
            gap = (target - current) / target * 100
        else:
            gap = (current - target) / target * 100
    else:
        gap = 0
    
    metric.gap_percent = f"{gap:.1f}%"

    # Determine styling based on priority and gap
    # Note: Priorities in model are 'High', 'Medium', 'Low' (PascalCase)
    # The views sometimes use 'high', 'medium', 'low' (lowercase)
    # Ensure consistency by checking both or converting to a standard case.
    # For now, matching the views' usage with 'High', 'Medium', 'Low'.
    if metric.priority == 'High':
        metric.priority_class = 'bg-red-100 text-red-500'
        metric.gap_class = 'bg-red-100 text-red-500' if gap < 0 else 'bg-green-100 text-green-500'
    elif metric.priority == 'Medium':
        metric.priority_class = 'bg-yellow-100 text-yellow-700'
        metric.gap_class = 'bg-yellow-100 text-yellow-700' if gap < 0 else 'bg-green-100 text-green-500'
    else: # Low priority
        metric.priority_class = 'bg-green-100 text-green-700'
        metric.gap_class = 'bg-green-100 text-green-700'

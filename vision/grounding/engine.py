from typing import List, Optional, Dict, Any, Tuple
from vision.observations.targets import TargetCandidate, GroundedTarget
from vision.perception_contract import PerceptionResult
from vision.grounding.models import (
    TargetSpec, 
    GroundingResult, 
    GroundingStatus, 
    GroundingCandidate,
    SpatialRelationship
)
from vision.grounding.scorers import (
    score_semantic_match, 
    score_role_match, 
    score_state_match,
    score_spatial_relationship
)

class GroundingEngine:
    """
    Stage 24 Advanced Grounding Engine.
    Transforms a TargetSpec and PerceptionResult into a GroundedTarget.
    """
    def __init__(self, confidence_threshold: float = 0.5, ambiguity_threshold: float = 0.1):
        self.confidence_threshold = confidence_threshold
        self.ambiguity_threshold = ambiguity_threshold
        
    def ground(self, spec: TargetSpec, observation: PerceptionResult) -> GroundingResult:
        if not observation or not observation.candidates:
            return GroundingResult(
                status=GroundingStatus.NOT_FOUND,
                reason="No candidates in observation.",
                observation_id=observation.observation_id if observation else None
            )
            
        candidates = list(observation.candidates)
        
        # 1. Candidate Generation & Filtering
        filtered_candidates = self._filter_candidates(spec, candidates, observation)
        
        if not filtered_candidates:
            return GroundingResult(
                status=GroundingStatus.NOT_FOUND,
                reason="No candidates survived filtering.",
                observation_id=observation.observation_id
            )
            
        # 2. Score Candidates
        scored_candidates = self._score_candidates(spec, filtered_candidates, observation)
        
        # Sort by score descending
        scored_candidates.sort(key=lambda x: x.score, reverse=True)
        
        # 3. Handle Ordinals (e.g. "eighth result")
        if spec.ordinal is not None:
            return self._handle_ordinal(spec, scored_candidates, observation)
            
        # 4. Handle Spatial Relationships
        if spec.relationship is not None:
            # We first need to ground the reference target
            ref_result = self.ground(spec.relationship.target_spec, observation)
            if ref_result.status == GroundingStatus.RESOLVED and ref_result.target:
                ref_candidate = ref_result.target.candidate
                # Rescore based on spatial relationship
                return self._ground_spatial(spec, scored_candidates, ref_candidate, observation)
            else:
                return GroundingResult(
                    status=GroundingStatus.NOT_FOUND,
                    reason="Could not ground reference target for spatial relationship.",
                    observation_id=observation.observation_id
                )
        
        # 5. Disambiguation
        return self._disambiguate(scored_candidates, observation)

    def _filter_candidates(
        self, spec: TargetSpec, candidates: List[TargetCandidate], observation: PerceptionResult
    ) -> List[TargetCandidate]:
        """Filter out candidates that are impossible matches (e.g., wrong window context)."""
        filtered = []
        for c in candidates:
            # Context-aware filtering
            if spec.application:
                # If target specified an app, and we know the candidate's app, filter
                # (Assuming we have app metadata in candidate properties)
                pass
                
            # State filtering (if strict)
            if not score_state_match(spec, c):
                continue
                
            # Bounds safety
            left, top, right, bottom = c.bbox
            if right <= left or bottom <= top:
                continue # invalid geometry
                
            filtered.append(c)
        return filtered

    def _score_candidates(
        self, spec: TargetSpec, candidates: List[TargetCandidate], observation: PerceptionResult
    ) -> List[GroundingCandidate]:
        scored = []
        for c in candidates:
            # Score components
            semantic_score = score_semantic_match(spec, c)
            role_score = score_role_match(spec, c)
            state_score = score_state_match(spec, c)
            
            # Source confidence factor
            source_conf = c.confidence
            
            # Combine scores (weights can be adjusted)
            # A good semantic match is very important. 
            total_score = (semantic_score * 0.6) + (role_score * 0.2) + (state_score * 0.1) + (source_conf * 0.1)
            
            # If semantic_score is 0 and we requested a semantic name, this candidate is likely completely wrong.
            if (spec.semantic_name or spec.text) and semantic_score == 0:
                total_score = 0.0
                
            scored.append(GroundingCandidate(
                candidate=c,
                score=total_score,
                confidence=total_score, # For simplicity, confidence is tied to score
                factors={
                    "semantic": semantic_score,
                    "role": role_score,
                    "state": state_score,
                    "source": source_conf
                }
            ))
        return scored
        
    def _handle_ordinal(
        self, spec: TargetSpec, scored_candidates: List[GroundingCandidate], observation: PerceptionResult
    ) -> GroundingResult:
        """Handle ordinals (e.g. '8th result') by sorting top candidates visually."""
        # Filter to only plausible candidates (score > threshold)
        plausible = [c for c in scored_candidates if c.score >= self.confidence_threshold]
        
        if not plausible:
            return GroundingResult(
                status=GroundingStatus.NOT_FOUND,
                reason="No plausible candidates to apply ordinal to.",
                observation_id=observation.observation_id
            )
            
        # Sort plausible candidates visually (top-to-bottom, left-to-right)
        def visual_sort_key(c: GroundingCandidate):
            left, top, right, bottom = c.candidate.bbox
            # Rough row-based grouping (e.g., 20px tolerance)
            row = top // 20
            return (row, left)
            
        plausible.sort(key=visual_sort_key)
        
        # 1-indexed
        idx = spec.ordinal - 1
        if 0 <= idx < len(plausible):
            selected = plausible[idx]
            return GroundingResult(
                status=GroundingStatus.RESOLVED,
                target=GroundedTarget(
                    candidate=selected.candidate,
                    resolution_method=f"ordinal_{spec.ordinal}",
                    alternatives=len(plausible) - 1
                ),
                confidence=selected.confidence,
                candidates=scored_candidates,
                observation_id=observation.observation_id,
                diagnostics={"ordinal_applied": spec.ordinal, "total_plausible": len(plausible)}
            )
        else:
            return GroundingResult(
                status=GroundingStatus.NOT_FOUND,
                reason=f"Ordinal {spec.ordinal} requested, but only {len(plausible)} plausible items found.",
                observation_id=observation.observation_id
            )

    def _ground_spatial(
        self, 
        spec: TargetSpec, 
        scored_candidates: List[GroundingCandidate], 
        ref_candidate: TargetCandidate, 
        observation: PerceptionResult
    ) -> GroundingResult:
        
        spatially_scored = []
        for sc in scored_candidates:
            if sc.candidate == ref_candidate:
                continue # Can't be relative to itself
                
            spatial_score = score_spatial_relationship(sc.candidate, spec.relationship, ref_candidate)
            new_score = (sc.score * 0.5) + (spatial_score * 0.5)
            
            spatially_scored.append(GroundingCandidate(
                candidate=sc.candidate,
                score=new_score,
                confidence=new_score,
                factors={**sc.factors, "spatial": spatial_score}
            ))
            
        spatially_scored.sort(key=lambda x: x.score, reverse=True)
        return self._disambiguate(spatially_scored, observation)

    def _disambiguate(self, scored_candidates: List[GroundingCandidate], observation: PerceptionResult) -> GroundingResult:
        # Filter out zero scores
        valid = [c for c in scored_candidates if c.score > 0]
        
        if not valid:
            return GroundingResult(
                status=GroundingStatus.NOT_FOUND,
                reason="No matching candidates.",
                observation_id=observation.observation_id
            )
            
        best = valid[0]
        if best.score < self.confidence_threshold:
            return GroundingResult(
                status=GroundingStatus.LOW_CONFIDENCE,
                reason=f"Best score {best.score:.2f} is below threshold {self.confidence_threshold}",
                candidates=valid,
                observation_id=observation.observation_id
            )
            
        if len(valid) > 1:
            runner_up = valid[1]
            if (best.score - runner_up.score) < self.ambiguity_threshold:
                return GroundingResult(
                    status=GroundingStatus.AMBIGUOUS,
                    reason=f"Ambiguity detected: Best score {best.score:.2f}, Runner up {runner_up.score:.2f}",
                    candidates=valid,
                    observation_id=observation.observation_id
                )
                
        return GroundingResult(
            status=GroundingStatus.RESOLVED,
            target=GroundedTarget(
                candidate=best.candidate,
                resolution_method="highest_score",
                alternatives=len(valid) - 1
            ),
            confidence=best.confidence,
            candidates=scored_candidates,
            observation_id=observation.observation_id,
            diagnostics={"best_score": best.score}
        )

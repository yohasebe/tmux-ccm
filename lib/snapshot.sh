#!/usr/bin/env bash
# ccm - snapshot save/load/list/delete (window-based)

# Save current project windows as a snapshot
ccm_snapshot_save() {
    local name="$1"

    if [[ -z "$name" ]]; then
        echo -n "Snapshot name: "
        read -r name
    fi
    [[ -z "$name" ]] && ccm_die "Snapshot name is required"

    ccm_init_dirs

    # Scan ALL sessions for ccm-tagged windows (not just current session)
    # This ensures save works from any context (dashboard popup, different session, etc.)
    local windows
    windows=$(tmux list-windows -a -F '#{window_index}	#{window_name}	#{@ccm_project}	#{@ccm_dir}' 2>/dev/null \
        | awk -F'\t' '$3 != ""' || true)

    if [[ -z "$windows" ]]; then
        ccm_die "No active projects to save"
    fi

    local projects="[]"
    while IFS=$'\t' read -r win_idx win_name project dir; do
        # Skip entries with empty project or dir
        [[ -z "$project" || -z "$dir" ]] && continue

        # Replace $HOME with ~ for portability
        dir="${dir/#$HOME/\~}"

        projects=$(echo "$projects" | jq \
            --arg name "$project" \
            --arg dir "$dir" \
            '. + [{"name": $name, "dir": $dir, "auto_start_claude": true}]')
    done <<< "$windows"

    local created
    created=$(date -u +"%Y-%m-%dT%H:%M:%S%z")

    local snapshot
    snapshot=$(jq -n \
        --arg name "$name" \
        --arg created "$created" \
        --argjson projects "$projects" \
        '{"version": 1, "name": $name, "created": $created, "projects": $projects}')

    local file="${CCM_SNAPSHOT_DIR}/${name}.json"
    echo "$snapshot" > "$file"
    ccm_info "Snapshot saved: $name ($file)"
}

# Load a snapshot and recreate project windows
ccm_snapshot_load() {
    local name="$1"

    if [[ -z "$name" ]]; then
        # Interactive selection with fzf
        local snapshots
        snapshots=$(ls "$CCM_SNAPSHOT_DIR"/*.json 2>/dev/null | xargs -I{} basename {} .json)
        [[ -z "$snapshots" ]] && ccm_die "No snapshots found"

        name=$(echo "$snapshots" | fzf --prompt="Select snapshot: " --height=10)
        [[ -z "$name" ]] && return
    fi

    local file="${CCM_SNAPSHOT_DIR}/${name}.json"
    [[ ! -f "$file" ]] && ccm_die "Snapshot not found: $name"

    local project_count
    project_count=$(jq '.projects | length' "$file")

    echo "Loading snapshot: $name ($project_count projects)"

    # Suppress autosave during load to prevent overwriting the source snapshot
    export _CCM_LOADING_SNAPSHOT=1

    for i in $(seq 0 $((project_count - 1))); do
        local proj_name proj_dir auto_start
        proj_name=$(jq -r ".projects[$i].name" "$file")
        proj_dir=$(jq -r ".projects[$i].dir" "$file")
        auto_start=$(jq -r ".projects[$i].auto_start_claude // true" "$file")

        # Skip null/empty entries
        [[ -z "$proj_name" || "$proj_name" == "null" ]] && continue
        [[ -z "$proj_dir" || "$proj_dir" == "null" ]] && continue

        proj_dir=$(ccm_expand_path "$proj_dir")

        if ccm_project_exists "$proj_name"; then
            ccm_warn "Project window already exists, skipping: $proj_name"
            continue
        fi

        if [[ ! -d "$proj_dir" ]]; then
            ccm_warn "Directory not found, skipping: $proj_name ($proj_dir)"
            continue
        fi

        ccm_add "$proj_dir" "$proj_name" "$auto_start"
    done

    unset _CCM_LOADING_SNAPSHOT

    # Save autosave after all projects are loaded
    if ! (ccm_snapshot_save "_autosave") 2>/dev/null; then
        ccm_warn "Failed to save autosave snapshot after load"
    fi

    ccm_info "Snapshot loaded: $name"
}

# List available snapshots
ccm_snapshot_list() {
    ccm_init_dirs

    local files
    files=$(ls "$CCM_SNAPSHOT_DIR"/*.json 2>/dev/null || true)

    if [[ -z "$files" ]]; then
        echo "No snapshots."
        return
    fi

    printf "${COLOR_BOLD}%-20s %-24s %s${COLOR_RESET}\n" "NAME" "CREATED" "PROJECTS"
    printf "%-20s %-24s %s\n" "----" "-------" "--------"

    for file in $files; do
        local name created count
        name=$(jq -r '.name' "$file")
        created=$(jq -r '.created' "$file")
        count=$(jq '.projects | length' "$file")
        printf "%-20s %-24s %d\n" "$name" "$created" "$count"
    done
}

# Delete a snapshot
ccm_snapshot_delete() {
    local name="$1"

    if [[ -z "$name" ]]; then
        local snapshots
        snapshots=$(ls "$CCM_SNAPSHOT_DIR"/*.json 2>/dev/null | xargs -I{} basename {} .json)
        [[ -z "$snapshots" ]] && ccm_die "No snapshots found"

        name=$(echo "$snapshots" | fzf --prompt="Delete snapshot: " --height=10)
        [[ -z "$name" ]] && return
    fi

    local file="${CCM_SNAPSHOT_DIR}/${name}.json"
    [[ ! -f "$file" ]] && ccm_die "Snapshot not found: $name"

    rm "$file"
    ccm_info "Snapshot deleted: $name"
}

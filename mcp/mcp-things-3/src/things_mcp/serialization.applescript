use framework "Foundation"
use scripting additions

on jsonText(value)
    set jsonData to current application's NSJSONSerialization's dataWithJSONObject:value options:0 |error|:(missing value)
    if jsonData is missing value then error "Cannot serialize automation result"
    return (current application's NSString's alloc()'s initWithData:jsonData encoding:4) as text
end jsonText

on putValue(output, keyName, value)
    if value is missing value then
        output's removeObjectForKey:keyName
    else
        output's setObject:value forKey:keyName
    end if
end putValue

on dateText(value)
    if value is missing value then return missing value
    set formatter to current application's NSDateFormatter's new()
    formatter's setLocale:(current application's NSLocale's localeWithLocaleIdentifier:"en_US_POSIX")
    formatter's setDateFormat:"yyyy-MM-dd'T'HH:mm:ssZZZZZ"
    return (formatter's stringFromDate:value) as text
end dateText

on localDate(y, m, d)
    set value to current date
    set day of value to 1
    set year of value to y
    set month of value to m
    set day of value to d
    set time of value to 0
    return value
end localDate

on snapshot(value, itemKind)
    set output to current application's NSMutableDictionary's new()
    tell application "Things3"
        my putValue(output, "id", id of value)
        my putValue(output, "title", name of value)
        if itemKind is "to-do" or itemKind is "selected" or itemKind is "project" then
            if class of value is project then
                set itemKind to "project"
            else
                set itemKind to "to-do"
            end if
            my putValue(output, "notes", notes of value)
            my putValue(output, "status", (status of value) as text)
            my putValue(output, "deadline", my dateText(due date of value))
            my putValue(output, "activation_date", my dateText(activation date of value))
            my putValue(output, "created_at", my dateText(creation date of value))
            my putValue(output, "modified_at", my dateText(modification date of value))
            my putValue(output, "completed_at", my dateText(completion date of value))
            my putValue(output, "canceled_at", my dateText(cancellation date of value))
            set parentProject to project of value
            set parentArea to area of value
            set parentID to missing value
            if parentProject is not missing value then set parentID to id of parentProject
            my putValue(output, "project_id", parentID)
            set parentID to missing value
            if parentArea is not missing value then set parentID to id of parentArea
            my putValue(output, "area_id", parentID)
        end if
        if itemKind is "tag" then
            my putValue(output, "keyboard_shortcut", keyboard shortcut of value)
            set parentValue to parent tag of value
            set parentID to missing value
            if parentValue is not missing value then set parentID to id of parentValue
            my putValue(output, "parent_tag_id", parentID)
        else if itemKind is not "list" then
            set tagValues to current application's NSMutableArray's new()
            repeat with oneTag in tags of value
                tagValues's addObject:((name of oneTag) as text)
            end repeat
            my putValue(output, "tags", tagValues)
        end if
    end tell
    my putValue(output, "kind", itemKind)
    return output
end snapshot

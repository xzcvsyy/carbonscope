select category, subtype, factor_value, count(*) as row_count
from emission_factors
group by category, subtype, factor_value
order by category, subtype;

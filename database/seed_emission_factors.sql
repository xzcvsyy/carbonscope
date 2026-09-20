-- =============================================================
-- CarbonScope — emission_factors seed data
-- Run this AFTER schema.sql. Values match the placeholder
-- factors currently used in the Estimate page frontend.
-- Replace with documented figures from your selected dataset(s)
-- once you've completed source review (SRS methodology step 1).
-- =============================================================

insert into emission_factors (category, subtype, label, factor_value, unit, source) values
    ('transport', 'car_gasoline', 'Car — gasoline', 0.12, 'kg CO2e per km', 'Placeholder — replace with documented source'),
    ('transport', 'motorcycle', 'Motorcycle', 0.09, 'kg CO2e per km', 'Placeholder — replace with documented source'),
    ('transport', 'traditional_jeep', 'Traditional jeep', 0.15, 'kg CO2e per km', 'Placeholder — replace with documented source'),
    ('transport', 'bicycle_walking', 'Bicycle / walking', 0.00, 'kg CO2e per km', 'Placeholder — replace with documented source'),

    ('electricity', 'grid_average', 'Grid electricity', 0.45, 'kg CO2e per kWh', 'Placeholder — replace with documented source'),

    ('devices', 'general_use', 'Digital device use', 0.10, 'kg CO2e per hr/day (weekly-normalized)', 'Placeholder — replace with documented source'),

    ('paper', 'general_use', 'Paper / material use', 0.02, 'kg CO2e per sheet', 'Placeholder — replace with documented source'),

    ('food', 'vegan', 'Vegan', 10.00, 'kg CO2e per week', 'Placeholder — replace with documented source'),
    ('food', 'vegetarian', 'Vegetarian', 15.00, 'kg CO2e per week', 'Placeholder — replace with documented source'),
    ('food', 'mixed_diet', 'Mixed diet', 22.00, 'kg CO2e per week', 'Placeholder — replace with documented source'),
    ('food', 'meat_heavy', 'Meat-heavy', 35.00, 'kg CO2e per week', 'Placeholder — replace with documented source');
